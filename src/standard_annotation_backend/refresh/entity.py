"""Refresh entity catalogs from configured GPI sources.

Fetched files are decoded by `decode_source_text`, parsed with the parser for
the source's format, staged as an inactive snapshot, and published atomically.
Sources removed from `config/sources.yaml` have their catalogs retired by a
separate job.
"""

import logging
from collections.abc import Callable, Mapping
from uuid import UUID

from standard_annotation_backend.domain.entities import (
    EntityCandidateConflictError,
    EntityCatalog,
    EntityCatalogCollisionError,
    EntityRefreshResult,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    RefreshKindName,
    SourceProvenance,
    refresh_failure_message,
)
from standard_annotation_backend.gpi.parser import GpiParseError, parse_gpi
from standard_annotation_backend.refresh.decoding import decode_source_text
from standard_annotation_backend.refresh.fetchers import SourceDocument
from standard_annotation_backend.refresh.kinds import (
    ProgressReporter,
    RefreshResult,
    TerminalRefreshError,
)
from standard_annotation_backend.refresh.sources import (
    GitHubEntitySource,
    GitHubSource,
    HttpsEntitySource,
    HttpsSource,
    RefreshSources,
)
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import Job, JobService

logger = logging.getLogger(__name__)

_LOGGED_ROW_ISSUES = 5
_PARSERS: Mapping[str, Callable[[str, SourceProvenance], EntityCatalog]] = {
    "gpi": parse_gpi
}


class EntityRefreshKind:
    """Refresh and retire entity catalogs.

    Implements both `RefreshKind` and `RefreshRetirer`.

    Args:
        sources: Configured sources for every kind.
        service: Stages, publishes, and retires catalogs.
        jobs: Records job failures.
    """

    def __init__(
        self,
        *,
        sources: RefreshSources,
        service: EntityRefreshService,
        jobs: JobService,
    ) -> None:
        self._sources = sources
        self._service = service
        self._jobs = jobs

    @property
    def name(self) -> RefreshKindName:
        """Return `entity`."""
        return RefreshKindName.ENTITY

    @property
    def job_type(self) -> JobType:
        """Return `entity_refresh`."""
        return JobType.ENTITY_REFRESH

    @property
    def terminal_errors(self) -> Mapping[type[Exception], RefreshFailureCode]:
        """Map catalog conflicts to their failure codes."""
        return {
            EntityCatalogCollisionError: RefreshFailureCode.CATALOG_COLLISION,
            EntityCandidateConflictError: RefreshFailureCode.CANDIDATE_CONFLICT,
        }

    def sources(self) -> Mapping[str, GitHubSource | HttpsSource]:
        """Return configured entity sources."""
        return self._sources.sources(RefreshKindName.ENTITY)

    def recover(self, job: Job) -> RefreshResult | None:
        """Finish from work an earlier attempt committed, without fetching.

        First, if this job already published its catalog, its stored result is
        returned. Second, if it committed staging, the staging is checked and
        published. Otherwise the source must be fetched.

        Raises:
            EntityCandidateConflictError: If committed staging belongs to another
                source or is incomplete or altered.
        """
        # Publication commits the catalog and its result together, so a stored
        # result means the earlier attempt stopped only before `succeed`.
        completed = self._service.completed(job.job_id)
        if completed is not None:
            return _published(completed)
        # Staging is committed before publication starts. Publishing it now
        # avoids fetching a file that may have changed since it was staged.
        resumed = self._service.resume(job_id=job.job_id, actor_id=job.requested_by)
        return None if resumed is None else _published(resumed.to_job_result())

    def unchanged(
        self, source_key: str, document: SourceDocument
    ) -> RefreshResult | None:
        """Return a result when the active catalog has the same locator and bytes."""
        result = self._service.unchanged(
            source_key=source_key,
            source_locator=document.source_locator,
            source_checksum=document.source_checksum,
        )
        if result is None:
            return None
        return RefreshResult(
            result=result.to_job_result(), progress={"unchanged": True}
        )

    def apply(
        self, job: Job, document: SourceDocument, progress: ProgressReporter
    ) -> RefreshResult:
        """Decode, parse, stage, and publish a new catalog.

        Staging and publication are separate transactions. If the worker stops
        between them, `recover` publishes the committed staging on redelivery.

        Raises:
            SourceError: If the bytes are not valid gzip or UTF-8.
            TerminalRefreshError: With `header` or `row_validation` if parsing
                fails; `failure_details` describes the problem.
        """
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
                RefreshFailureCode(error.code), error.details
            ) from None
        progress(_catalog_progress("staging", catalog), catalog.warnings)
        self._service.stage(
            job_id=job.job_id, source_key=document.source_key, catalog=catalog
        )
        progress(_catalog_progress("publishing", catalog), catalog.warnings)
        published = self._service.publish(job_id=job.job_id, actor_id=job.requested_by)
        return _published(published.to_job_result())

    def fail(
        self,
        job_id: UUID,
        failure_code: RefreshFailureCode,
        failure_details: dict[str, object] | None,
    ) -> None:
        """Mark the job failed and delete its staging in the same transaction."""
        self._jobs.fail_refresh(
            job_id,
            error=refresh_failure_message(RefreshKindName.ENTITY),
            failure_code=failure_code,
            failure_details=failure_details,
            cleanup=lambda uow: uow.entities.cleanup_terminal_staging(job_id),
        )

    def after_terminal(self, source_key: str) -> None:
        """Entity refreshes have no follow-up work."""

    def retire(self, job: Job, source_key: str) -> RefreshResult:
        """Retire a source's catalog unless the source is configured again.

        The source may have been added back to the sources file after the
        retirement job was created; then nothing is retired.
        """
        result = self._service.retire(
            job_id=job.job_id,
            source_key=source_key,
            configured=source_key in self._sources.keys(RefreshKindName.ENTITY),
            actor_id=job.requested_by,
        )
        return RefreshResult(
            result=result.to_job_result(),
            progress={
                "retired": result.retired,
                "removed_count": len(result.removal_impacts),
            },
        )


def _catalog_progress(phase: str, catalog: EntityCatalog) -> dict[str, object]:
    """Build a job's progress for one phase from catalog counts only."""
    return {
        "phase": phase,
        "source_record_count": len(catalog.records),
        "active_identifier_count": len(
            {record.entity.db_object_id for record in catalog.records}
        ),
        "warning_count": len(catalog.warnings),
    }


def _published(result: dict[str, object]) -> RefreshResult:
    """Convert a stored publication result into job progress and warnings.

    Raises:
        ValueError: If the stored result is malformed. This is not a terminal
            refresh failure, so the run is retried.
    """
    EntityRefreshResult.from_job_result(result)
    warnings = result["warnings"]
    removal_impacts = result["removal_impacts"]
    if not isinstance(warnings, list) or not all(
        isinstance(warning, str) for warning in warnings
    ):
        raise ValueError("invalid durable entity refresh warnings")
    if not isinstance(removal_impacts, list):
        raise ValueError("invalid durable entity refresh removal impacts")
    return RefreshResult(
        result=result,
        progress={
            "source_record_count": result.get("source_record_count", 0),
            "active_identifier_count": result.get("active_identifier_count", 0),
            "added_count": result.get("added_count", 0),
            "retained_count": result.get("retained_count", 0),
            "removed_count": result.get("removed_count", 0),
            "warning_count": len(warnings),
            "removal_impact_count": len(removal_impacts),
        },
        warnings=tuple(warnings),
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
