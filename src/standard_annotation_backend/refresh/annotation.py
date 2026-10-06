"""Replace groups' annotations from configured GPAD sources.

`annotation_refresh` jobs replace a `gpad_imported` group's annotations. They
skip a file that matches the group's last import when the group has not changed
since. `annotation_cutover` jobs never skip, require every record to be imported,
and move the group to `sab_managed` permanently. One class serves both job types.
"""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import Engine

from standard_annotation_backend.domain.annotation_management import (
    AnnotationImportConflictError,
    AnnotationManagementMode,
    CutoverRejectedError,
    GroupSabManagedError,
    NoValidAnnotationsError,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    RefreshKindName,
    refresh_failure_message,
)
from standard_annotation_backend.gpad.parser import GpadHeaderError
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    try_advisory_lock,
)
from standard_annotation_backend.refresh.decoding import decode_source_text
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
from standard_annotation_backend.services.annotation_refresh_service import (
    AnnotationRefreshService,
)
from standard_annotation_backend.services.job_service import Job, JobService


class AnnotationRefreshBusyError(RuntimeError):
    """Report that another GPAD job for the same group is running.

    The job does not fail: `RefreshRunner` re-raises this error and Celery
    retries the task later, by which time the other job has usually finished.
    """

    def __init__(self) -> None:
        super().__init__("another GPAD job for this group is running")


class AnnotationRefreshKind:
    """Run GPAD refresh or cutover jobs; implements `RefreshKind`.

    Each instance runs one job type, chosen by `cutover`.

    Args:
        engine: Engine for the per-group lock's dedicated connection.
        sources: Configured sources for every kind.
        service: Stages and publishes imports.
        jobs: Records job failures.
        cutover: `True` to run `annotation_cutover` jobs, `False` to run
            `annotation_refresh` jobs.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        sources: RefreshSources,
        service: AnnotationRefreshService,
        jobs: JobService,
        cutover: bool,
    ) -> None:
        self._engine = engine
        self._sources = sources
        self._service = service
        self._jobs = jobs
        self._cutover = cutover

    @property
    def name(self) -> RefreshKindName:
        """Return `annotation`."""
        return RefreshKindName.ANNOTATION

    @property
    def job_type(self) -> JobType:
        """Return `annotation_cutover` or `annotation_refresh`."""
        return (
            JobType.ANNOTATION_CUTOVER if self._cutover else JobType.ANNOTATION_REFRESH
        )

    @property
    def terminal_errors(self) -> Mapping[type[Exception], RefreshFailureCode]:
        """Map errors that fail the job without details to their failure codes."""
        return {
            GroupSabManagedError: RefreshFailureCode.GROUP_SAB_MANAGED,
            AnnotationImportConflictError: RefreshFailureCode.INVALID_PARAMETERS,
        }

    def sources(self) -> Mapping[str, GitHubSource | HttpsSource]:
        """Return configured annotation sources."""
        return self._sources.sources(RefreshKindName.ANNOTATION)

    def recover(self, job: Job) -> RefreshResult | None:
        """Return the result of a job that published before it was interrupted.

        Unpublished staging is not resumed; `apply` stages the document afresh.
        """
        result = self._service.published_result(job.job_id)
        if result is None:
            return None
        return RefreshResult(
            result=result.to_job_result(), progress=result.to_progress()
        )

    def unchanged(
        self, source_key: str, document: SourceDocument
    ) -> RefreshResult | None:
        """Return a result that skips an ordinary refresh, if nothing would change.

        Returns:
            A result pointing to the group's last import when the document
            matches it and the group has not changed since. `None` when the
            document must be imported, and always for cutovers.
        """
        if self._cutover:
            return None
        unchanged = self._service.unchanged(
            source_key=source_key,
            group_key=self._sources.annotation_group(source_key),
            source_checksum=document.source_checksum,
        )
        return (
            None
            if unchanged is None
            else RefreshResult(result=unchanged.to_job_result())
        )

    def apply(
        self, job: Job, document: SourceDocument, progress: ProgressReporter
    ) -> RefreshResult:
        """Stage and publish a document while holding the group's lock.

        Raises:
            AnnotationRefreshBusyError: If another job for the group holds the lock.
            GroupSabManagedError: If the group is SAB-managed.
            AnnotationImportConflictError: If the job is not a GPAD job.
            SourceError: If the bytes are not valid gzip or UTF-8.
            TerminalRefreshError: With `header`, `no_valid_annotations`, or
                `cutover_rejected`.
        """
        group_key = self._sources.annotation_group(document.source_key)
        with try_advisory_lock(
            self._engine, LockNamespace.ANNOTATION_GROUP, group_key
        ) as acquired:
            if not acquired:
                raise AnnotationRefreshBusyError
            if (
                self._service.group_mode(group_key)
                is AnnotationManagementMode.SAB_MANAGED
            ):
                raise GroupSabManagedError(group_key)
            text = decode_source_text(document.content)
            progress({"phase": "staging"})
            try:
                report = self._service.stage(
                    job_id=job.job_id,
                    source_key=document.source_key,
                    group_key=group_key,
                    is_cutover=self._cutover,
                    provenance=document.provenance,
                    text=text,
                )
                progress(
                    {"phase": "publishing", "records_rejected": report.issue_count}
                )
                result = self._service.publish(
                    job_id=job.job_id, actor_id=job.requested_by
                )
            except GpadHeaderError as error:
                raise TerminalRefreshError(
                    RefreshFailureCode.HEADER, {"message": error.message}
                ) from None
            except NoValidAnnotationsError as error:
                raise TerminalRefreshError(
                    RefreshFailureCode.NO_VALID_ANNOTATIONS, error.report.to_json()
                ) from None
            except CutoverRejectedError as error:
                raise TerminalRefreshError(
                    RefreshFailureCode.CUTOVER_REJECTED, error.report.to_json()
                ) from None
        return RefreshResult(
            result=result.to_job_result(), progress=result.to_progress()
        )

    def fail(
        self,
        job_id: UUID,
        failure_code: RefreshFailureCode,
        failure_details: dict[str, object] | None,
    ) -> None:
        """Mark the job failed and delete its staging in the same transaction."""
        self._jobs.fail_refresh(
            job_id,
            error=refresh_failure_message(RefreshKindName.ANNOTATION),
            failure_code=failure_code,
            failure_details=failure_details,
            cleanup=lambda uow: uow.annotation_imports.cleanup_terminal_staging(job_id),
        )

    def after_terminal(self, source_key: str) -> None:
        """Do nothing; GPAD jobs have no follow-up work."""
