"""Refresh ontologies from configured OBO sources and prune old snapshots.

A refresh parses the OBO file, stages a complete inactive snapshot, and activates
it while applying safe term replacements to annotations. Only one process at a
time may stage, activate, or prune snapshots of one ontology. Each ontology has
a PostgreSQL advisory lock that a process takes only if it is free. A refresh
that finds it held raises `OntologyRefreshBusyError`, and Celery retries the task
later instead of waiting.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Engine

from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.ontology import (
    OntologyDocument,
    OntologyKey,
    OntologyParseError,
    OntologyRefreshResult,
)
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    RefreshKindName,
    job_source_key,
    refresh_failure_message,
)
from standard_annotation_backend.ontology.definitions import ontology_definition
from standard_annotation_backend.ontology.obo_parser import parse_obo
from standard_annotation_backend.persistence.locks import ontology_refresh_lock
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh.fetchers import SourceDocument
from standard_annotation_backend.refresh.kinds import (
    ProgressReporter,
    RefreshResult,
    TerminalRefreshError,
)
from standard_annotation_backend.refresh.sources import (
    GitHubSource,
    HttpsSource,
    RefreshSources,
)
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyCandidateConflictError,
    OntologyRefreshService,
)

logger = logging.getLogger(__name__)


class OntologyRefreshBusyError(RuntimeError):
    """Report that another process holds the ontology's refresh lock."""


class OntologyRefreshKind:
    """Refresh configured ontologies.

    Args:
        engine: Engine for the per-ontology lock's dedicated connection.
        sources: Configured sources for every kind.
        unit_of_work_factory: Opens transactions for ontology services.
        jobs: Records job failures.
        enqueue_prune: Schedules pruning after a job finishes. `None` prunes in
            this process instead.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        sources: RefreshSources,
        unit_of_work_factory: UnitOfWorkFactory,
        jobs: JobService,
        enqueue_prune: Callable[[str], None] | None,
    ) -> None:
        self._engine = engine
        self._sources = sources
        self._jobs = jobs
        self._enqueue_prune = enqueue_prune
        # One service per supported ontology, each bound to that ontology's
        # identifier prefixes and closure predicates.
        self._services = {
            key: OntologyRefreshService(unit_of_work_factory, ontology_definition(key))
            for key in OntologyKey
        }

    @property
    def name(self) -> RefreshKindName:
        """Return `ontology`."""
        return RefreshKindName.ONTOLOGY

    @property
    def job_type(self) -> JobType:
        """Return `ontology_refresh`."""
        return JobType.ONTOLOGY_REFRESH

    @property
    def terminal_errors(self) -> Mapping[type[Exception], RefreshFailureCode]:
        """Map candidate conflicts, such as a newer active snapshot, to a code."""
        return {OntologyCandidateConflictError: RefreshFailureCode.CANDIDATE_CONFLICT}

    def sources(self) -> Mapping[str, GitHubSource | HttpsSource]:
        """Return configured ontology sources."""
        return self._sources.sources(RefreshKindName.ONTOLOGY)

    def recover(self, job: Job) -> RefreshResult | None:
        """Return the stored result if this job already activated its snapshot.

        A key that is not a supported ontology has nothing to recover; the runner
        then reports it as an unknown source.
        """
        key = _ontology_key(job_source_key(job.parameters))
        if key is None:
            return None
        # Activation commits the snapshot and its result together, so a stored
        # result means an earlier attempt stopped only before marking the job
        # succeeded.
        completed = self._services[key].completed(job.job_id)
        return None if completed is None else _stored(completed)

    def unchanged(
        self, source_key: str, document: SourceDocument
    ) -> RefreshResult | None:
        """Return a result when the active snapshot came from the same document."""
        key = OntologyKey(source_key)
        ontology_document = _ontology_document(key, document)
        if not self._services[key].matches_active(ontology_document):
            return None
        return _stored(
            OntologyRefreshResult.unchanged(ontology_document).to_job_result()
        )

    def apply(
        self, job: Job, document: SourceDocument, progress: ProgressReporter
    ) -> RefreshResult:
        """Parse, stage, and activate a new snapshot under the ontology's lock.

        Raises:
            OntologyRefreshBusyError: If another process holds the lock. This is
                not a terminal failure, so the run is retried later.
            TerminalRefreshError: With `invalid_document` if the OBO is invalid.
        """
        key = OntologyKey(document.source_key)
        service = self._services[key]
        ontology_document = _ontology_document(key, document)
        # Stage and activate under the per-ontology lock, so two jobs cannot
        # activate snapshots out of order and pruning cannot delete a candidate.
        with ontology_refresh_lock(self._engine, key) as acquired:
            if not acquired:
                # The job stays running; Celery retries the whole task later.
                raise OntologyRefreshBusyError
            # Another job may have activated this same document while this one
            # was fetching, so check again now that the lock is held.
            if service.matches_active(ontology_document):
                # Report it the same way as the runner's own unchanged check.
                stored = _stored(
                    OntologyRefreshResult.unchanged(ontology_document).to_job_result()
                )
                return replace(stored, progress={**stored.progress, "unchanged": True})
            try:
                snapshot = parse_obo(ontology_document, ontology_definition(key))
            except OntologyParseError as error:
                # An invalid file stays invalid, so retrying cannot help.
                raise TerminalRefreshError(
                    RefreshFailureCode.INVALID_DOCUMENT, {"message": str(error)}
                ) from None
            service.stage(
                job_id=job.job_id, document=ontology_document, snapshot=snapshot
            )
            result = service.activate(job_id=job.job_id, actor_id=job.requested_by)
        return _stored(result.to_job_result())

    def fail(
        self,
        job_id: UUID,
        failure_code: RefreshFailureCode,
        failure_details: dict[str, object] | None,
    ) -> None:
        """Mark the job failed and audit why."""
        self._jobs.fail_refresh(
            job_id,
            error=refresh_failure_message(RefreshKindName.ONTOLOGY),
            failure_code=failure_code,
            failure_details=failure_details,
        )

    def after_terminal(self, source_key: str) -> None:
        """Prune old snapshot data now that this ontology's job has finished.

        Celery workers enqueue `sab.ontology.prune`, which retries while the lock
        is busy. The CLI prunes in its own process and skips pruning when the
        lock is busy; the next finished job prunes instead.
        """
        if _ontology_key(source_key) is None:
            return
        if self._enqueue_prune is not None:
            self._enqueue_prune(source_key)
            return
        try:
            if not self.prune(source_key):
                logger.info(
                    "Ontology pruning skipped because the ontology is busy: "
                    "ontology_key=%s",
                    source_key,
                )
        except Exception as error:
            # The job already finished and committed its result, so a pruning
            # failure must not make the caller report the refresh as failed.
            # Pruning is only cleanup: the next finished ontology job prunes
            # again and removes whatever this attempt left behind. Log the
            # exception type only, since details could expose infrastructure.
            logger.error(
                "Ontology pruning failed: ontology_key=%s failure_type=%s",
                source_key,
                type(error).__name__,
            )

    def prune(self, source_key: str) -> bool:
        """Delete unneeded term and closure rows for one ontology.

        Returns:
            `False` if another process holds the ontology's lock, otherwise `True`.
            An unsupported key has nothing to prune and also returns `True`.
        """
        key = _ontology_key(source_key)
        if key is None:
            return True
        with ontology_refresh_lock(self._engine, key) as acquired:
            if not acquired:
                return False
            self._services[key].prune(pruned_at=datetime.now(UTC))
        return True


def _ontology_key(source_key: str) -> OntologyKey | None:
    """Return the ontology for a source key, or `None` if it is not supported."""
    try:
        return OntologyKey(source_key)
    except ValueError:
        return None


def _ontology_document(key: OntologyKey, document: SourceDocument) -> OntologyDocument:
    """Convert a fetched document into the ontology parser's input."""
    return OntologyDocument(
        ontology_key=key,
        content=document.content,
        source_type=document.source_type,
        source_locator=document.source_locator,
        source_revision=document.source_revision,
        source_checksum=document.source_checksum,
        fetched_at=document.fetched_at,
    )


def _stored(result: dict[str, object]) -> RefreshResult:
    """Build job progress and warnings from a stored refresh result."""
    findings = result.get("findings")
    finding_count = len(findings) if isinstance(findings, list) else 0
    ontology_warnings = result.get("ontology_warnings")
    warning_count = len(ontology_warnings) if isinstance(ontology_warnings, list) else 0
    warnings: list[str] = []
    if warning_count:
        noun = "warning" if warning_count == 1 else "warnings"
        warnings.append(
            f"Ontology refresh completed with {warning_count} ontology {noun}"
        )
    if finding_count:
        warnings.append(f"Ontology refresh completed with {finding_count} findings")
    return RefreshResult(
        result=result,
        progress={
            "annotation_scan_count": result.get("annotation_scan_count", 0),
            "annotation_update_count": result.get("annotation_update_count", 0),
            "annotation_skip_count": result.get("annotation_skip_count", 0),
            "finding_count": finding_count,
            "ontology_warning_count": warning_count,
        },
        warnings=tuple(warnings),
    )
