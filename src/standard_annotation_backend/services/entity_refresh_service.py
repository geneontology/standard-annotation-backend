"""Run entity refresh and retirement jobs for `RefreshRunner` from GPI sources.

Fetched files are decoded by `decode_source_text`, parsed with the parser for
the source's format, staged as an inactive snapshot, and published atomically.
Staging and publication commit separately, so a redelivered job publishes its
committed staging without fetching again. Sources removed from
`config/sources.yaml` have their catalogs retired by a separate job.
"""

import logging
from collections.abc import Callable, Mapping
from uuid import UUID

from standard_annotation_backend.domain.entities import (
    EntityCatalog,
    EntityCatalogRetirementResult,
    EntityRefreshResult,
    EntityRefreshUnchangedResult,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import (
    ProgressReporter,
    RefreshFailureCode,
    RefreshKindName,
    RefreshOutcome,
    SourceDocument,
    SourceProvenance,
    TerminalRefreshError,
)
from standard_annotation_backend.gpi.parser import GpiParseError, parse_gpi
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.refresh.decoding import decode_source_text
from standard_annotation_backend.refresh.sources import (
    GitHubEntitySource,
    HttpsEntitySource,
    RefreshSources,
)
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job

logger = logging.getLogger(__name__)

_LOGGED_ROW_ISSUES = 5
_PARSERS: Mapping[str, Callable[[str, SourceProvenance], EntityCatalog]] = {
    "gpi": parse_gpi
}


class EntityRefreshService:
    """Refresh and retire entity catalogs, committing each step with its audit events.

    Implements both `RefreshKind` and `RefreshRetirer`.

    Args:
        unit_of_work_factory: Opens the transactions for staging, publication,
            and retirement.
        sources: Configured sources for every kind.
    """

    def __init__(
        self, unit_of_work_factory: UnitOfWorkFactory, sources: RefreshSources
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._sources = sources

    @property
    def name(self) -> RefreshKindName:
        """Return `entity`."""
        return RefreshKindName.ENTITY

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `entity_refresh`."""
        return frozenset({JobType.ENTITY_REFRESH})

    def recover(self, job: Job) -> RefreshOutcome | None:
        """Finish from work an earlier attempt committed, without fetching.

        First, if this job already published its catalog, its stored result is
        returned. Second, if it committed staging, the staging is checked and
        published. Otherwise the source must be fetched.

        Raises:
            EntityCandidateConflictError: If committed staging belongs to another
                source or is incomplete or altered.
            StoredDataError: If the stored result is malformed; the run is
                retried.
        """
        # Publication commits the catalog and its result together, so a stored
        # result means the earlier attempt stopped only before `succeed`.
        completed = self._completed(job.job_id)
        if completed is not None:
            return _published(completed)
        # Staging is committed before publication starts. Publishing it now
        # avoids fetching a file that may have changed since it was staged.
        resumed = self._resume(job_id=job.job_id, actor_id=job.requested_by)
        return None if resumed is None else _published(resumed)

    def apply(
        self, job: Job, document: SourceDocument, report: ProgressReporter
    ) -> RefreshOutcome:
        """Decode, parse, stage, and publish a catalog unless it is already active.

        The active catalog is unchanged when it came from the same locator and
        bytes; then nothing is staged or published. Staging and publication are
        separate transactions. If the worker stops between them, `recover`
        publishes the committed staging on redelivery.

        Raises:
            SourceError: If the bytes are not valid gzip or UTF-8.
            TerminalRefreshError: With `header` or `row_validation` if parsing
                fails; `failure_details` describes the problem.
        """
        current = self._current(
            source_key=document.source_key,
            source_locator=document.source_locator,
            source_checksum=document.source_checksum,
        )
        if current is not None:
            return RefreshOutcome(result=current.to_job_result(), unchanged=True)
        source = self._sources.source(RefreshKindName.ENTITY, document.source_key)
        source_format = (
            source.format
            if isinstance(source, (GitHubEntitySource, HttpsEntitySource))
            else "gpi"
        )
        text = decode_source_text(document.content)
        try:
            catalog = _PARSERS[source_format](text, document.provenance)
        except GpiParseError as error:
            # Log a summary without field values; the job keeps full details.
            logger.error(
                "Entity refresh rejected its source: job_id=%s failure_code=%s%s",
                job.job_id,
                error.code,
                _failure_log_summary(error),
            )
            raise TerminalRefreshError(
                failure_code=RefreshFailureCode(error.code),
                failure_details=error.details,
            ) from None
        report("staging", _catalog_counts(catalog), catalog.warnings)
        self._stage(job_id=job.job_id, source_key=document.source_key, catalog=catalog)
        report("publishing", _catalog_counts(catalog), catalog.warnings)
        published = self._publish_job(job_id=job.job_id, actor_id=job.requested_by)
        return _published(published)

    def discard_staging(self, uow: SqlAlchemyUnitOfWork, job_id: UUID) -> None:
        """Delete the job's staging in the failure transaction."""
        uow.entities.cleanup_terminal_staging(job_id)

    def after_terminal(self, source_key: str) -> None:
        """Do nothing; entity refreshes have no follow-up work."""

    def retire(self, job: Job, source_key: str) -> RefreshOutcome:
        """Retire a source's catalog unless the source is configured again.

        The source may have been added back to the sources file after the
        retirement job was created; then nothing is retired.
        """
        result = self._retire(
            job_id=job.job_id,
            source_key=source_key,
            configured=source_key in self._sources.keys(RefreshKindName.ENTITY),
            actor_id=job.requested_by,
        )
        return RefreshOutcome(
            result=result.to_job_result(),
            counts={"removed_count": len(result.removal_impacts)},
        )

    def _stage(
        self,
        *,
        job_id: UUID,
        source_key: str,
        catalog: EntityCatalog,
    ) -> None:
        """Store a parsed catalog as a job's inactive staging and commit it.

        If the job already staged the same catalog, the existing staging is kept.

        Raises:
            EntityCandidateConflictError: If the job already staged a different
                catalog, or the job has finished.
        """
        with self._unit_of_work_factory() as uow:
            uow.entities.stage(job_id, source_key, catalog)
            uow.commit()

    def _retire(
        self, *, job_id: UUID, source_key: str, configured: bool, actor_id: str
    ) -> EntityCatalogRetirementResult:
        """Retire a source's active catalog and record its audit event, then commit.

        If this job already retired the catalog, for example before its task was
        delivered again, the stored result is returned and no audit event is added.
        """
        with self._unit_of_work_factory() as uow:
            result, changed = uow.entities.retire(
                job_id, source_key, configured=configured
            )
            if changed:
                AuditService(uow.audit).record_entity_retired(
                    actor_id=actor_id,
                    job_id=job_id,
                    details={
                        "source_key": source_key,
                        "snapshot_id": str(result.snapshot_id),
                        "removed_count": len(result.removal_impacts),
                    },
                )
            uow.commit()
            return result

    def _current(
        self, *, source_key: str, source_locator: str, source_checksum: str
    ) -> EntityRefreshUnchangedResult | None:
        """Return a result when the source's active catalog has identical content.

        Both the locator and the checksum must match, so pointing a source at a new
        location always publishes a new snapshot.
        """
        with self._unit_of_work_factory() as uow:
            snapshot = uow.entities.active_snapshot(source_key)
            if (
                snapshot is None
                or snapshot.source_locator != source_locator
                or snapshot.source_checksum != source_checksum
            ):
                return None
            return EntityRefreshUnchangedResult(
                source_key=source_key,
                snapshot_id=snapshot.snapshot_id,
                source_checksum=source_checksum,
            )

    def _publish_job(self, *, job_id: UUID, actor_id: str) -> EntityRefreshResult:
        """Make a job's staged catalog active and record its audit event, then commit.

        If this job already published its catalog, the stored result is returned
        and no audit event is added.
        """
        with self._unit_of_work_factory() as uow:
            return self._publish(uow, job_id=job_id, actor_id=actor_id)

    def _resume(self, *, job_id: UUID, actor_id: str) -> EntityRefreshResult | None:
        """Publish a job's committed staging without fetching the source again.

        Returns `None` when the job has no staging, so the source must be
        fetched. Staged rows are checked against the stored record counts and
        catalog checksum in the same transaction that publishes them.

        Raises:
            EntityCandidateConflictError: If the staging belongs to another source
                or is incomplete or altered. The active catalog is not changed.
        """
        with self._unit_of_work_factory() as uow:
            if not uow.entities.resumable(job_id):
                return None
            return self._publish(uow, job_id=job_id, actor_id=actor_id)

    def _publish(
        self, uow: SqlAlchemyUnitOfWork, *, job_id: UUID, actor_id: str
    ) -> EntityRefreshResult:
        """Publish in an open unit of work, record the audit event, and commit."""
        uow.entities.lock_job(job_id)
        completed = uow.entities.completed(job_id)
        if completed is not None:
            return completed
        result = uow.entities.publish(job_id)
        AuditService(uow.audit).record_entity_refreshed(
            actor_id=actor_id,
            job_id=job_id,
            details={
                "snapshot_id": str(result.snapshot_id),
                "source_key": result.source_key,
                "source_record_count": result.source_record_count,
                "active_identifier_count": result.active_identifier_count,
                "added_count": result.added_count,
                "retained_count": result.retained_count,
                "removed_count": result.removed_count,
                "warning_count": len(result.warnings),
            },
        )
        uow.commit()
        return result

    def _completed(self, job_id: UUID) -> EntityRefreshResult | None:
        """Return a job's stored publication result, or `None` if not yet published."""
        with self._unit_of_work_factory() as uow:
            return uow.entities.completed(job_id)


def _catalog_counts(catalog: EntityCatalog) -> dict[str, int]:
    """Return the catalog counts reported while staging and publishing."""
    return {
        "source_record_count": len(catalog.records),
        "active_identifier_count": len(
            {record.entity.db_object_id for record in catalog.records}
        ),
        "warning_count": len(catalog.warnings),
    }


def _published(result: EntityRefreshResult) -> RefreshOutcome:
    """Convert a publication result into the job's outcome."""
    return RefreshOutcome(
        result=result.to_job_result(),
        counts={
            "source_record_count": result.source_record_count,
            "active_identifier_count": result.active_identifier_count,
            "added_count": result.added_count,
            "retained_count": result.retained_count,
            "removed_count": result.removed_count,
            "warning_count": len(result.warnings),
            "removal_impact_count": len(result.removal_impacts),
        },
        warnings=result.warnings,
    )


def _failure_log_summary(error: Exception) -> str:
    """Describe a GPI parsing failure for the worker log, without field values.

    Header failures add their message. Row failures add the number of invalid
    rows and the line and category of the first 5. A `validation` row names its
    invalid fields only, because validation messages can repeat the rejected
    value; other categories add their reason. The job record keeps the full
    details.
    """
    if not isinstance(error, GpiParseError):
        return ""
    if error.code == "header":
        return f" message={error.message}"
    summaries = []
    for issue in error.issues[:_LOGGED_ROW_ISSUES]:
        reason = (
            "fields " + ", ".join(field.field for field in issue.fields)
            if issue.fields
            else issue.message or ""
        )
        summaries.append(f"line {issue.line_number} {issue.category}: {reason}")
    return f" issue_count={len(error.issues)} issues=[{' | '.join(summaries)}]"
