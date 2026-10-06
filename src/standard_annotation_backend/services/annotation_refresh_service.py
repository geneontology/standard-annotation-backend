"""Replace groups' annotations from configured GPAD sources for `RefreshRunner`.

`annotation_refresh` jobs replace a `gpad_imported` group's annotations. They
skip a file that matches the group's last import when the group has not changed
since. `annotation_cutover` jobs never skip, require every record to be imported,
and move the group to `sab_managed` permanently. One service runs both job types.
Staging and publication commit in separate transactions.
"""

from collections.abc import Iterator
from itertools import islice
from uuid import UUID

from standard_annotation_backend.domain.annotation_management import (
    UNKNOWN_DB_OBJECT_ID,
    AnnotationManagementMode,
    AnnotationRefreshResult,
    AnnotationRefreshUnchanged,
    CutoverRejectedError,
    GroupSabManagedError,
    NoValidAnnotationsError,
    RejectionReport,
    RejectionReportBuilder,
    unknown_subject_rejection,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import (
    ProgressReporter,
    RefreshFailureCode,
    RefreshKindName,
    RefreshOutcome,
    RetryableRefreshError,
    SourceDocument,
    SourceProvenance,
    TerminalRefreshError,
)
from standard_annotation_backend.gpad.parser import (
    GpadDocument,
    GpadHeaderError,
    ParsedAnnotation,
)
from standard_annotation_backend.persistence.locks import (
    AdvisoryTryLock,
    LockNamespace,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.refresh.decoding import decode_source_text
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job

_STAGING_BATCH_SIZE = 5_000


class AnnotationRefreshBusyError(RetryableRefreshError):
    """Report that another GPAD job for the same group is running.

    The job does not fail: `RefreshRunner` re-raises this error and Celery
    retries the task later, by which time the other job has usually finished.
    """

    def __init__(self) -> None:
        super().__init__("another GPAD job for this group is running")


class AnnotationRefreshService:
    """Run GPAD refresh and cutover jobs, committing each step separately.

    Implements `RefreshKind` for both `annotation_refresh` and
    `annotation_cutover` jobs.

    Args:
        unit_of_work_factory: Opens the transactions for staging and publication.
        sources: Configured sources for every kind; maps a GPAD source to its
            group.
        try_lock: Takes the per-group lock.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        sources: RefreshSources,
        try_lock: AdvisoryTryLock,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._sources = sources
        self._try_lock = try_lock

    @property
    def name(self) -> RefreshKindName:
        """Return `annotation`."""
        return RefreshKindName.ANNOTATION

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `annotation_refresh` and `annotation_cutover`."""
        return frozenset({JobType.ANNOTATION_REFRESH, JobType.ANNOTATION_CUTOVER})

    def recover(self, job: Job) -> RefreshOutcome | None:
        """Return the result of a job that published before it was interrupted.

        Unpublished staging is not resumed; `apply` stages the document afresh.
        """
        result = self._published_result(job.job_id)
        if result is None:
            return None
        return RefreshOutcome(
            result=result.to_job_result(), counts=result.to_progress()
        )

    def apply(
        self, job: Job, document: SourceDocument, report: ProgressReporter
    ) -> RefreshOutcome:
        """Stage and publish a document while holding the group's lock.

        An ordinary refresh is skipped, before taking the lock, when the
        document matches the group's last import and the group has not changed
        since. A cutover is never skipped.

        Raises:
            AnnotationRefreshBusyError: If another job for the group holds the lock.
            GroupSabManagedError: If the group is SAB-managed.
            AnnotationImportConflictError: If the job is not a GPAD job.
            SourceError: If the bytes are not valid gzip or UTF-8.
            TerminalRefreshError: With `header`, `no_valid_annotations`, or
                `cutover_rejected`.
        """
        is_cutover = job.job_type is JobType.ANNOTATION_CUTOVER
        group_key = self._sources.annotation_group(document.source_key)
        if not is_cutover:
            current = self._current(
                source_key=document.source_key,
                group_key=group_key,
                source_checksum=document.source_checksum,
            )
            if current is not None:
                return RefreshOutcome(result=current.to_job_result(), unchanged=True)
        with self._try_lock(LockNamespace.ANNOTATION_GROUP, group_key) as acquired:
            if not acquired:
                raise AnnotationRefreshBusyError
            if self._group_mode(group_key) is AnnotationManagementMode.SAB_MANAGED:
                raise GroupSabManagedError(group_key)
            text = decode_source_text(document.content)
            report("staging")
            try:
                rejections = self._stage(
                    job_id=job.job_id,
                    source_key=document.source_key,
                    group_key=group_key,
                    is_cutover=is_cutover,
                    provenance=document.provenance,
                    text=text,
                )
                report("publishing", {"records_rejected": rejections.issue_count})
                result = self._publish(job_id=job.job_id, actor_id=job.requested_by)
            except GpadHeaderError as error:
                raise TerminalRefreshError(
                    failure_code=RefreshFailureCode.HEADER,
                    failure_details={"message": error.message},
                ) from None
            except NoValidAnnotationsError as error:
                raise TerminalRefreshError(
                    failure_code=RefreshFailureCode.NO_VALID_ANNOTATIONS,
                    failure_details=error.report.to_json(),
                ) from None
            except CutoverRejectedError as error:
                raise TerminalRefreshError(
                    failure_code=RefreshFailureCode.CUTOVER_REJECTED,
                    failure_details=error.report.to_json(),
                ) from None
        return RefreshOutcome(
            result=result.to_job_result(), counts=result.to_progress()
        )

    def discard_staging(self, uow: SqlAlchemyUnitOfWork, job_id: UUID) -> None:
        """Delete the job's staging in the failure transaction."""
        uow.annotation_imports.cleanup_terminal_staging(job_id)

    def after_terminal(self, source_key: str) -> None:
        """Do nothing; GPAD jobs have no follow-up work."""

    def _group_mode(self, group_key: str) -> AnnotationManagementMode:
        """Return whether GPAD or SAB manages a group's annotations."""
        with self._unit_of_work_factory() as uow:
            return uow.annotation_imports.group_mode(group_key)

    def _stage(
        self,
        *,
        job_id: UUID,
        source_key: str,
        group_key: str,
        is_cutover: bool,
        provenance: SourceProvenance,
        text: str,
    ) -> RejectionReport:
        """Read a GPAD document, stage its valid annotations, and commit.

        Staging stores the annotations in separate tables until `_publish`
        replaces the group's annotations with them. Any staging left by an
        earlier, interrupted attempt of the job is replaced. Annotations whose
        `db_object_id` is not an active entity are rejected. Nothing is committed
        when the document has an invalid header, yields no valid annotations, or
        is a cutover with any rejection.

        Returns:
            The report of rejected records.

        Raises:
            GpadHeaderError: If the header is invalid.
            NoValidAnnotationsError: If no annotation could be staged.
            CutoverRejectedError: If `is_cutover` and any record was rejected.
            AnnotationImportConflictError: If the job does not exist or is not a
                GPAD job.
        """
        builder = RejectionReportBuilder()
        staged = 0
        with self._unit_of_work_factory() as uow:
            imports = uow.annotation_imports
            imports.lock_job(job_id)
            imports.discard_unpublished(job_id)
            with GpadDocument(text) as document:
                for batch in _batches(document.items()):
                    parsed: list[ParsedAnnotation] = []
                    for item in batch:
                        if isinstance(item, ParsedAnnotation):
                            parsed.append(item)
                        else:
                            builder.add(item)
                    known = imports.active_db_object_ids(
                        {item.annotation.db_object_id for item in parsed}
                    )
                    accepted: list[ParsedAnnotation] = []
                    for item in parsed:
                        if item.annotation.db_object_id in known:
                            accepted.append(item)
                        else:
                            builder.add(
                                unknown_subject_rejection(
                                    item.line_number, item.annotation.db_object_id
                                )
                            )
                    imports.stage_annotations(job_id, accepted)
                    staged += len(accepted)
                metadata = document.metadata
                data_rows = document.data_rows
            report = builder.build()
            if staged == 0:
                raise NoValidAnnotationsError(report)
            if is_cutover and report.issue_count:
                raise CutoverRejectedError(report)
            imports.record_staged(
                job_id=job_id,
                source_key=source_key,
                group_key=group_key,
                is_cutover=is_cutover,
                provenance=provenance,
                source_metadata=metadata,
                data_rows=data_rows,
                annotations_staged=staged,
                report=report,
            )
            uow.commit()
        return report

    def _current(
        self, *, source_key: str, group_key: str, source_checksum: str
    ) -> AnnotationRefreshUnchanged | None:
        """Return a result that lets an ordinary refresh skip an unchanged file.

        A refresh can be skipped only when the group is `gpad_imported`, its last
        published import has the same `source_checksum`, the group has no local
        changes since that import, and that import rejected no annotations as
        `unknown_db_object_id`. Local changes include edits, comments, and change
        sets; see `AnnotationImportRepository.has_local_changes`. Those
        rejections are retried because the entity catalog may have changed even
        when the file has not.

        Returns:
            The skip result, or `None` when the file must be imported.
        """
        with self._unit_of_work_factory() as uow:
            imports = uow.annotation_imports
            if (
                imports.group_mode(group_key)
                is not AnnotationManagementMode.GPAD_IMPORTED
            ):
                return None
            last = imports.last_import(group_key)
            if (
                last is None
                or last.source_checksum != source_checksum
                or _has_unknown_subject_rejections(last.rejection_report)
                or imports.has_local_changes(group_key, last.job_id)
            ):
                return None
            return AnnotationRefreshUnchanged(
                source_key=source_key,
                group_key=group_key,
                import_job_id=last.job_id,
                source_checksum=source_checksum,
            )

    def _published_result(self, job_id: UUID) -> AnnotationRefreshResult | None:
        """Return a job's publication result, or `None` if it has not published."""
        with self._unit_of_work_factory() as uow:
            return uow.annotation_imports.published_result(job_id)

    def _publish(self, *, job_id: UUID, actor_id: str) -> AnnotationRefreshResult:
        """Publish a job's staged import with its audit event, then commit.

        If the job already published, its stored result is returned and no
        audit event is added.

        Raises:
            AnnotationImportConflictError: If the job does not exist, is not a
                GPAD job, or has no staged import.
            GroupSabManagedError: If the group is already SAB-managed.
            CutoverRejectedError: If a cutover's staged subject is no longer an
                active entity.
        """
        with self._unit_of_work_factory() as uow:
            existing = uow.annotation_imports.published_result(job_id)
            if existing is not None:
                return existing
            result = uow.annotation_imports.publish(job_id, actor_id=actor_id)
            AuditService(uow.audit).record_annotation_import_published(
                is_cutover=result.is_cutover,
                actor_id=actor_id,
                job_id=job_id,
                details={
                    "source_key": result.source_key,
                    "group_key": result.group_key,
                    "mode": result.mode.value,
                    **result.to_progress(),
                },
            )
            uow.commit()
            return result


def _has_unknown_subject_rejections(report: dict[str, object]) -> bool:
    """Report whether a stored rejection report counts any unknown-entity rejection."""
    by_code = report.get("by_code")
    return isinstance(by_code, dict) and bool(by_code.get(UNKNOWN_DB_OBJECT_ID))


def _batches[T](items: Iterator[T]) -> Iterator[list[T]]:
    """Split streamed items into lists of at most 5,000."""
    while batch := list(islice(items, _STAGING_BATCH_SIZE)):
        yield batch
