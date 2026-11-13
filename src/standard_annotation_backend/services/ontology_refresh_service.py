"""Refresh ontologies from configured OBO sources and prune old snapshots.

A refresh parses the OBO file, stages a complete inactive snapshot, and then
activates it. Activation changes no annotations; it queues a term replacement job
for all groups, which replaces obsolete terms and alternative IDs in annotations
later. Only one process at a time may stage, activate, or prune snapshots of one
ontology. Each ontology has a PostgreSQL advisory lock that a process takes only
if it is free. A refresh that finds it held raises `OntologyRefreshBusyError`,
and Celery retries the task later instead of waiting.
"""

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.ontology import (
    STORED_ONTOLOGY_REFRESH_RESULT,
    OntologyDefinition,
    OntologyDocument,
    OntologyParseError,
    OntologyRefreshResult,
    OntologySnapshot,
    OntologyTerm,
    OntologyVersion,
    StoredOntologyRefresh,
    compute_closure,
    missing_required_subsets,
    ontology_refresh_warnings,
)
from standard_annotation_backend.domain.refresh import (
    ProgressReporter,
    RefreshFailureCode,
    RefreshKindName,
    RefreshOutcome,
    RetryableRefreshError,
    SourceDocument,
    TerminalRefreshError,
    job_source_key,
)
from standard_annotation_backend.ontology.obo_parser import parse_obo
from standard_annotation_backend.persistence.locks import (
    AdvisoryTryLock,
    LockNamespace,
)
from standard_annotation_backend.persistence.models import (
    OntologyMetadataRecord,
    OntologyTermRecord,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.term_replacement_service import (
    queue_term_replacement,
)

logger = logging.getLogger(__name__)


class OntologyRefreshBusyError(RetryableRefreshError):
    """Report that another process holds the ontology's refresh lock."""


class OntologyRefreshService:
    """Refresh and prune every supported ontology.

    Implements `RefreshKind`. One service serves every configured ontology; the
    job's source key selects the ontology's definition in `config/sources.yaml`.

    Args:
        unit_of_work_factory: Opens the transactions for staging, activation,
            and pruning.
        sources: Configured sources, which say how to read each ontology.
        try_lock: Takes the per-ontology lock.
        enqueue_prune: Schedules pruning after a job finishes. `None` prunes in
            this process instead.
        dispatch_replacement: Sends a queued term replacement job to a worker
            after an activation commits. `None` leaves the job queued without
            sending it.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        sources: RefreshSources,
        try_lock: AdvisoryTryLock,
        enqueue_prune: Callable[[str], None] | None,
        *,
        dispatch_replacement: Callable[[Job], None] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._sources = sources
        self._try_lock = try_lock
        self._enqueue_prune = enqueue_prune
        self._dispatch_replacement = dispatch_replacement

    @property
    def name(self) -> RefreshKindName:
        """Return `ontology`."""
        return RefreshKindName.ONTOLOGY

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `ontology_refresh`."""
        return frozenset({JobType.ONTOLOGY_REFRESH})

    def recover(self, job: Job) -> RefreshOutcome | None:
        """Return the stored result if this job already activated its snapshot.

        A key that is not a configured ontology has nothing to recover; the runner
        then reports it as an unknown source.

        Raises:
            StoredDataError: If the stored result is malformed; the run is retried.
        """
        if not self._is_configured(job_source_key(job.parameters)):
            return None
        # Activation commits the snapshot and its result together, so a stored
        # result means an earlier attempt stopped only before marking the job
        # succeeded.
        completed = self._completed(job.job_id)
        return None if completed is None else _stored(completed)

    def apply(
        self, job: Job, document: SourceDocument, report: ProgressReporter
    ) -> RefreshOutcome:
        """Parse, stage, and activate a new snapshot under the ontology's lock.

        A document that is already the active snapshot, read with the same
        identifier prefixes and closure predicates, is not applied. This is
        checked before taking the lock, so such a document never waits on another
        job, and again once the lock is held.

        Raises:
            OntologyRefreshBusyError: If another process holds the lock. This is
                not a terminal failure, so the run is retried later.
            TerminalRefreshError: With `invalid_document` if the OBO is invalid or
                does not declare every subset listed in the ontology's
                `required_subsets` setting.
            OntologyCandidateConflictError: If the job already staged a different
                source, or a snapshot staged after this job's is already active.
        """
        definition = self._sources.ontology_definition(document.source_key)
        ontology_document = _ontology_document(definition.key, document)
        if self._matches_active(ontology_document, definition):
            return _unchanged(ontology_document)
        # Stage and activate under the per-ontology lock, so two jobs cannot
        # activate snapshots out of order and pruning cannot delete a candidate.
        with self._try_lock(LockNamespace.ONTOLOGY, definition.key) as acquired:
            if not acquired:
                # The job stays running; Celery retries the whole task later.
                raise OntologyRefreshBusyError
            # Another job may have activated this same document while this one
            # was fetching, so check again now that the lock is held.
            if self._matches_active(ontology_document, definition):
                return _unchanged(ontology_document)
            try:
                snapshot = parse_obo(ontology_document, definition)
            except OntologyParseError as error:
                # An invalid file stays invalid, so retrying cannot help.
                raise TerminalRefreshError(
                    failure_code=RefreshFailureCode.INVALID_DOCUMENT,
                    failure_details={"message": str(error)},
                ) from None
            missing = missing_required_subsets(snapshot, definition)
            if missing:
                raise TerminalRefreshError(
                    failure_code=RefreshFailureCode.INVALID_DOCUMENT,
                    failure_details={
                        "message": "ontology source does not declare the subsets "
                        + ", ".join(missing)
                    },
                )
            self._stage(
                job_id=job.job_id, document=ontology_document, snapshot=snapshot
            )
            result = self._activate(
                definition, job_id=job.job_id, actor_id=job.requested_by
            )
        return _stored(result.to_stored())

    def discard_staging(self, uow: SqlAlchemyUnitOfWork, job_id: UUID) -> None:
        """Ontology candidates are removed by pruning, not on failure."""

    def after_terminal(self, source_key: str) -> None:
        """Prune old snapshot data now that this ontology's job has finished.

        Celery workers enqueue `sabe.ontology.prune`, which retries while the lock
        is busy. The CLI prunes in its own process and skips pruning when the
        lock is busy; the next finished job prunes instead.
        """
        if not self._is_configured(source_key):
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
            An unconfigured key has nothing to prune and also returns `True`.
        """
        if not self._is_configured(source_key):
            return True
        with self._try_lock(LockNamespace.ONTOLOGY, source_key) as acquired:
            if not acquired:
                return False
            self._prune(source_key, pruned_at=datetime.now(UTC))
        return True

    def _is_configured(self, key: str) -> bool:
        """Return whether `key` is an ontology in the sources file."""
        return key in self._sources.keys(RefreshKindName.ONTOLOGY)

    def _matches_active(
        self, document: OntologyDocument, definition: OntologyDefinition
    ) -> bool:
        """Return whether the active snapshot came from this document and definition."""
        with self._unit_of_work_factory() as unit_of_work:
            return unit_of_work.ontologies.matches_active_source(document, definition)

    def _completed(self, job_id: UUID) -> StoredOntologyRefresh | None:
        """Return the stored result if this job completed ontology activation.

        Raises:
            RuntimeError: If the job's snapshot is active but has no stored result.
            StoredDataError: If the stored result does not have its expected shape.
        """
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.ontologies.get_by_job(job_id)
            if record is None:
                return None
            if record.refresh_result is not None:
                return STORED_ONTOLOGY_REFRESH_RESULT.load(record.refresh_result)
            if record.active:
                raise RuntimeError("active ontology snapshot has no durable result")
            return None

    def _prune(self, key: str, *, pruned_at: datetime) -> tuple[UUID, ...]:
        """Delete eligible snapshot term and closure rows and commit the transaction.

        Args:
            key: Ontology whose snapshots are pruned.
            pruned_at: Time to record on each snapshot that is pruned.

        Returns:
            Version identifiers for snapshots pruned by this call.
        """
        with self._unit_of_work_factory() as unit_of_work:
            pruned = unit_of_work.ontologies.prune_candidates(key, pruned_at)
            unit_of_work.commit()
        return pruned

    def _stage(
        self,
        *,
        job_id: UUID,
        document: OntologyDocument,
        snapshot: OntologySnapshot,
    ) -> None:
        """Persist and commit one complete inactive candidate snapshot.

        If the job already staged the same source, its existing candidate is kept.

        Raises:
            OntologyCandidateConflictError: If the job already staged a different
                source.
            OntologySnapshotPrunedError: If the job's existing candidate was
                pruned.
        """
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.ontologies.stage(
                job_id=job_id,
                document=document,
                snapshot=snapshot,
                closure_rows=compute_closure(snapshot),
            )
            # Reading the counts raises if a kept candidate was already pruned.
            unit_of_work.ontologies.term_count(record.version_id)
            unit_of_work.ontologies.closure_count(record.version_id)
            unit_of_work.commit()

    def _activate(
        self, definition: OntologyDefinition, *, job_id: UUID, actor_id: str
    ) -> OntologyRefreshResult:
        """Make a staged snapshot the active one and record the result.

        Activation changes no annotations and takes no annotation lock, so
        annotation writes continue while it runs. The activation, its stored
        result, its audit event, and a queued term replacement job for all groups
        are committed together, and the job is then sent to a worker.

        Raises:
            OntologyCandidateConflictError: If a snapshot staged after this job's
                candidate is already active.
        """
        with self._unit_of_work_factory() as unit_of_work:
            candidate, _previous = unit_of_work.ontologies.lock_activation_state(job_id)
            candidate_terms = _terms(
                unit_of_work.ontologies.list_terms(candidate.version_id)
            )
            version = _ontology_version(
                candidate,
                term_count=len(candidate_terms),
                closure_count=unit_of_work.ontologies.closure_count(
                    candidate.version_id
                ),
            )
            warnings = ontology_refresh_warnings(
                candidate_terms, definition.identifier_prefixes
            )
            if candidate.active:
                return OntologyRefreshResult(version, ontology_warnings=warnings)
            activated = unit_of_work.ontologies.activate(candidate.version_id)
            replacement = queue_term_replacement(
                unit_of_work, group_key=None, requested_by=actor_id
            )
            version = replace(version, active=activated.active)
            result = OntologyRefreshResult(version, ontology_warnings=warnings)
            unit_of_work.ontologies.record_refresh_result(
                candidate, result.to_job_result()
            )
            AuditService(unit_of_work.audit).record_ontology_refreshed(
                actor_id=actor_id,
                job_id=job_id,
                details={
                    "ontology_key": candidate.ontology_key,
                    "ontology_version_id": str(candidate.version_id),
                    "source_revision": candidate.source_revision,
                    "term_count": version.term_count,
                    "closure_count": version.closure_count,
                    "ontology_warning_count": len(result.ontology_warnings),
                },
            )
            unit_of_work.commit()
        self._dispatch(replacement)
        return result

    def _dispatch(self, job: Job) -> None:
        """Send a replacement job to a worker, leaving it queued if sending fails.

        A failure is logged. The next activation of any ontology reuses the queued
        job and sends it again.
        """
        if self._dispatch_replacement is None:
            return
        try:
            self._dispatch_replacement(job)
        except Exception as error:
            logger.error(
                "Term replacement dispatch failed: job_id=%s failure_type=%s",
                job.job_id,
                type(error).__name__,
            )


def _terms(records: list[OntologyTermRecord]) -> dict[str, OntologyTerm]:
    """Convert stored term rows to ontology terms indexed by identifier."""
    return {
        record.term_id: OntologyTerm(
            record.term_id,
            record.obsolete,
            tuple(record.replaced_by),
            tuple(record.consider),
            subsets=tuple(record.subsets),
        )
        for record in records
    }


def _ontology_version(
    record: OntologyMetadataRecord,
    *,
    term_count: int,
    closure_count: int,
) -> OntologyVersion:
    """Build an ontology-version result from stored metadata and row counts."""
    return OntologyVersion(
        version_id=record.version_id,
        ontology_key=record.ontology_key,
        source_type=record.source_type,
        source_locator=record.source_locator,
        source_revision=record.source_revision,
        source_checksum=record.source_checksum,
        document_version=record.document_version,
        loaded_predicates=tuple(record.loaded_predicates),
        fetched_at=record.fetched_at,
        term_count=term_count,
        closure_count=closure_count,
        active=record.active,
    )


def _ontology_document(key: str, document: SourceDocument) -> OntologyDocument:
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


def _unchanged(document: OntologyDocument) -> RefreshOutcome:
    """Build the outcome for a document that is already the active snapshot."""
    result = OntologyRefreshResult.for_active_document(document).to_stored()
    return replace(_stored(result), unchanged=True)


def _stored(stored: StoredOntologyRefresh) -> RefreshOutcome:
    """Build the job's outcome from a stored refresh result."""
    warning_count = len(stored.ontology_warnings)
    warnings: tuple[str, ...] = ()
    if warning_count:
        noun = "warning" if warning_count == 1 else "warnings"
        warnings = (f"Ontology refresh completed with {warning_count} ontology {noun}",)
    return RefreshOutcome(
        result=STORED_ONTOLOGY_REFRESH_RESULT.dump(stored),
        counts={"ontology_warning_count": warning_count},
        warnings=warnings,
    )
