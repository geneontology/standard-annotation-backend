"""Run entity refresh jobs for `RefreshRunner` from GPI sources.

Fetched files are decoded by `decode_source_text`, parsed with the parser for
the source's format, and stored and activated as a snapshot in the job's
transaction. Sources removed from `config/sources.yaml` are retired when every
entity source is refreshed (see `RefreshStartService`).
"""

import logging
from collections.abc import Callable, Mapping

from standard_annotation_backend.domain.entities import (
    EntityCatalog,
    EntityCatalogCollisionError,
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
from standard_annotation_backend.domain.snapshots import SnapshotKind, SourceContent
from standard_annotation_backend.gpi.parser import GpiParseError, parse_gpi
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork
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
    """Refresh entity catalogs, recording an audit event for each publication.

    Implements `RefreshKind`.

    Args:
        sources: Configured sources for every kind.
    """

    def __init__(self, sources: RefreshSources) -> None:
        self._sources = sources

    @property
    def name(self) -> RefreshKindName:
        """Return `entity`."""
        return RefreshKindName.ENTITY

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `entity_refresh`."""
        return frozenset({JobType.ENTITY_REFRESH})

    def apply(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        job: Job,
        document: SourceDocument,
        report: ProgressReporter,
    ) -> RefreshOutcome:
        """Store and activate a catalog snapshot unless it is already active.

        A document whose content the active catalog already holds, read with the
        same reader version, is not stored again. Either way, entity catalog
        snapshots that are no longer kept are pruned.

        Raises:
            SourceError: If the bytes are not valid gzip or UTF-8.
            TerminalRefreshError: With `header` or `row_validation` if parsing
                fails; `failure_details` describes the problem.
            EntityCatalogCollisionError: If another source's active catalog
                lists one of the catalog's identifiers.
        """
        snapshots = unit_of_work.snapshots
        content = SourceContent.of(RefreshKindName.ENTITY, document.provenance)
        active = snapshots.matching_active(
            SnapshotKind.ENTITY, document.source_key, content
        )
        if active is not None:
            snapshots.prune(SnapshotKind.ENTITY)
            unchanged = EntityRefreshUnchangedResult(
                source_key=document.source_key,
                snapshot_id=active.snapshot_id,
                source_checksum=document.source_checksum,
            )
            return RefreshOutcome(result=unchanged.to_job_result(), unchanged=True)
        catalog = self._parse(job, document)
        counts = _catalog_counts(catalog)
        report("staging", counts, catalog.warnings)
        record = snapshots.create(
            SnapshotKind.ENTITY, document.source_key, job.job_id, document.provenance
        )
        unit_of_work.entities.insert(record.snapshot_id, catalog)
        report("publishing", counts, catalog.warnings)
        unit_of_work.entities.lock_activation()
        collisions = unit_of_work.entities.collisions(
            record.snapshot_id, document.source_key
        )
        if collisions:
            raise EntityCatalogCollisionError(collisions)
        previous = snapshots.activate(record)
        new_ids = {entry.entity.db_object_id for entry in catalog.records}
        old_ids = (
            set()
            if previous is None
            else unit_of_work.entities.identifiers(previous.snapshot_id)
        )
        result = EntityRefreshResult(
            snapshot_id=record.snapshot_id,
            source_key=document.source_key,
            source_type=record.source_type,
            source_locator=record.source_locator,
            source_revision=record.source_revision,
            source_checksum=record.source_checksum,
            source_record_count=len(catalog.records),
            active_identifier_count=len(new_ids),
            added_count=len(new_ids - old_ids),
            retained_count=len(new_ids & old_ids),
            removed_count=len(old_ids - new_ids),
            warnings=catalog.warnings,
        )
        AuditService(unit_of_work.audit).record_entity_refreshed(
            actor_id=job.requested_by,
            job_id=job.job_id,
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
        snapshots.prune(SnapshotKind.ENTITY)
        return _published(result)

    def _parse(self, job: Job, document: SourceDocument) -> EntityCatalog:
        """Decode and parse a fetched catalog with its source's format.

        Raises:
            SourceError: If the bytes are not valid gzip or UTF-8.
            TerminalRefreshError: With `header` or `row_validation`.
        """
        source = self._sources.source(RefreshKindName.ENTITY, document.source_key)
        source_format = (
            source.format
            if isinstance(source, (GitHubEntitySource, HttpsEntitySource))
            else "gpi"
        )
        text = decode_source_text(document.content)
        try:
            return _PARSERS[source_format](text, document.provenance)
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


def _catalog_counts(catalog: EntityCatalog) -> dict[str, int]:
    """Return the catalog counts reported in the `staging` and `publishing` phases."""
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
