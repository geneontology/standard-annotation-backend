"""Stage and publish GPAD annotation imports, each step in its own transaction."""

from collections.abc import Iterator
from itertools import islice
from uuid import UUID

from standard_annotation_backend.domain.annotation_management import (
    UNKNOWN_DB_OBJECT_ID,
    AnnotationManagementMode,
    AnnotationRefreshResult,
    AnnotationRefreshUnchanged,
    CutoverRejectedError,
    NoValidAnnotationsError,
    RejectionReport,
    RejectionReportBuilder,
    unknown_subject_rejection,
)
from standard_annotation_backend.domain.refresh import SourceProvenance
from standard_annotation_backend.gpad.parser import GpadDocument, ParsedAnnotation
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.audit_service import AuditService

_STAGING_BATCH_SIZE = 5_000


class AnnotationRefreshService:
    """Stage, check, and publish GPAD imports, committing each step separately."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def group_mode(self, group_key: str) -> AnnotationManagementMode:
        """Return whether GPAD or SAB manages a group's annotations."""
        with self._unit_of_work_factory() as uow:
            return uow.annotation_imports.group_mode(group_key)

    def sab_managed_groups(self) -> frozenset[str]:
        """Return every group whose annotations are managed by SAB."""
        with self._unit_of_work_factory() as uow:
            return uow.annotation_imports.sab_managed_groups()

    def stage(
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

        Staging stores the annotations in separate tables until `publish`
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

    def unchanged(
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

    def published_result(self, job_id: UUID) -> AnnotationRefreshResult | None:
        """Return a job's publication result, or `None` if it has not published."""
        with self._unit_of_work_factory() as uow:
            return uow.annotation_imports.published_result(job_id)

    def publish(self, *, job_id: UUID, actor_id: str) -> AnnotationRefreshResult:
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
