"""Replace groups' annotations from configured GPAD sources for `RefreshRunner`.

`annotation_refresh` jobs replace a `gpad_imported` group's annotations. They
skip a file that matches the group's last import, when that import was checked
with the installed schema package version and the group has not changed since.
`annotation_cutover` jobs never skip, require every record to be imported,
and move the group to `sabe_managed` permanently. One service runs both job types.
Staging and publication happen in the job's transaction. Each publication also
queues a term replacement job for the group.
"""

from datetime import UTC, datetime
from itertools import batched
from uuid import UUID

from standard_annotation_backend.domain.annotation_management import (
    STORED_REJECTION_REPORT,
    AnnotationManagementMode,
    AnnotationRefreshResult,
    AnnotationRefreshUnchanged,
    CutoverRejectedError,
    GroupSabeManagedError,
    NoValidAnnotationsError,
    RejectionReportBuilder,
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
from standard_annotation_backend.domain.schema_artifacts import SCHEMA_PACKAGE_VERSION
from standard_annotation_backend.gpad.parser import (
    GpadDocument,
    GpadHeaderError,
    ParsedAnnotation,
)
from standard_annotation_backend.persistence.models import AnnotationImportRecord
from standard_annotation_backend.persistence.repositories.annotation_imports import (
    AnnotationImportRepository,
    StagedImport,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from standard_annotation_backend.refresh.decoding import decode_source_text
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.term_replacement_service import (
    queue_term_replacement,
)

_STAGING_BATCH_SIZE = 5_000


class AnnotationRefreshService:
    """Run GPAD refresh and cutover jobs in the job's transaction.

    Implements `RefreshKind` for both `annotation_refresh` and
    `annotation_cutover` jobs.

    Args:
        sources: Configured sources for every kind; maps a GPAD source to its
            group.
    """

    def __init__(self, sources: RefreshSources) -> None:
        self._sources = sources

    @property
    def name(self) -> RefreshKindName:
        """Return `annotation`."""
        return RefreshKindName.ANNOTATION

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `annotation_refresh` and `annotation_cutover`."""
        return frozenset({JobType.ANNOTATION_REFRESH, JobType.ANNOTATION_CUTOVER})

    def apply(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        job: Job,
        document: SourceDocument,
        report: ProgressReporter,
    ) -> RefreshOutcome:
        """Stage and publish a document in the job's transaction.

        An ordinary refresh is skipped when the document matches the group's
        last import, that import was checked with the installed schema package
        version, and the group has not changed since. A cutover is never
        skipped. A group has at most one unfinished GPAD job, so no other job
        stages or publishes it meanwhile.

        Raises:
            GroupSabeManagedError: If the group is SABE-managed.
            SourceError: If the bytes are not valid gzip or UTF-8.
            TerminalRefreshError: With `header`, `no_valid_annotations`, or
                `cutover_rejected`.
        """
        is_cutover = job.job_type is JobType.ANNOTATION_CUTOVER
        group_key = self._sources.annotation_group(document.source_key)
        imports = unit_of_work.annotation_imports
        if not is_cutover:
            current = _current(
                imports,
                source_key=document.source_key,
                group_key=group_key,
                source_checksum=document.source_checksum,
            )
            if current is not None:
                return RefreshOutcome(result=current.to_job_result(), unchanged=True)
        if imports.group_mode(group_key) is AnnotationManagementMode.SABE_MANAGED:
            raise GroupSabeManagedError(group_key)
        text = decode_source_text(document.content)
        report("staging")
        try:
            staged = _stage(
                imports,
                job_id=job.job_id,
                source_key=document.source_key,
                group_key=group_key,
                is_cutover=is_cutover,
                provenance=document.provenance,
                text=text,
            )
            report("publishing", {"records_rejected": staged.report.issue_count})
            record = imports.publish(staged, actor_id=job.requested_by)
        except GpadHeaderError as error:
            raise TerminalRefreshError(
                failure_code=RefreshFailureCode.HEADER,
                failure_details={"message": error.message},
            ) from None
        except NoValidAnnotationsError as error:
            raise TerminalRefreshError(
                failure_code=RefreshFailureCode.NO_VALID_ANNOTATIONS,
                failure_details=STORED_REJECTION_REPORT.dump(error.report),
            ) from None
        except CutoverRejectedError as error:
            raise TerminalRefreshError(
                failure_code=RefreshFailureCode.CUTOVER_REJECTED,
                failure_details=STORED_REJECTION_REPORT.dump(error.report),
            ) from None
        result = _import_result(record)
        AuditService(unit_of_work.audit).record_annotation_import_published(
            is_cutover=result.is_cutover,
            actor_id=job.requested_by,
            job_id=job.job_id,
            details={
                "source_key": result.source_key,
                "group_key": result.group_key,
                "mode": result.mode.value,
                **result.to_progress(),
            },
        )
        # Queued last: the queue lock is held until this transaction commits.
        replacement = queue_term_replacement(
            unit_of_work, group_key=result.group_key, requested_by=job.requested_by
        )
        return RefreshOutcome(
            result=result.to_job_result(),
            counts=result.to_progress(),
            follow_up_job_ids=(replacement.job_id,),
        )


def _stage(
    imports: AnnotationImportRepository,
    *,
    job_id: UUID,
    source_key: str,
    group_key: str,
    is_cutover: bool,
    provenance: SourceProvenance,
    text: str,
) -> StagedImport:
    """Read a GPAD document and stage its valid annotations for publication.

    Raises:
        GpadHeaderError: If the header is invalid.
        NoValidAnnotationsError: If no annotation could be staged.
        CutoverRejectedError: If `is_cutover` and any record was rejected.
    """
    builder = RejectionReportBuilder()
    staged = 0
    with GpadDocument(text) as document:
        for batch in batched(document.items(), _STAGING_BATCH_SIZE, strict=False):
            parsed: list[ParsedAnnotation] = []
            for item in batch:
                if isinstance(item, ParsedAnnotation):
                    parsed.append(item)
                else:
                    builder.add(item)
            imports.stage_annotations(job_id, parsed)
            staged += len(parsed)
        metadata = document.metadata
        data_rows = document.data_rows
    report = builder.build()
    if staged == 0:
        raise NoValidAnnotationsError(report)
    if is_cutover and report.issue_count:
        raise CutoverRejectedError(report)
    return StagedImport(
        job_id=job_id,
        source_key=source_key,
        group_key=group_key,
        is_cutover=is_cutover,
        provenance=provenance,
        source_metadata=metadata,
        data_rows=data_rows,
        annotations_staged=staged,
        report=report,
        schema_version=SCHEMA_PACKAGE_VERSION,
        staged_at=datetime.now(UTC),
    )


def _current(
    imports: AnnotationImportRepository,
    *,
    source_key: str,
    group_key: str,
    source_checksum: str,
) -> AnnotationRefreshUnchanged | None:
    """Return a result that lets an ordinary refresh skip an unchanged file.

    A refresh can be skipped only when the group is `gpad_imported`, its last
    published import has the same `source_checksum` and was checked with the
    installed schema package version, and the group has no local changes since
    that import. Local changes include edits, comments, and change sets; see
    `AnnotationImportRepository.has_local_changes`. A different schema version
    can change which rows are accepted, so it requires a new import.

    Returns:
        The skip result, or `None` when the file must be imported.
    """
    if imports.group_mode(group_key) is not AnnotationManagementMode.GPAD_IMPORTED:
        return None
    last = imports.last_import(group_key)
    if (
        last is None
        or last.source_checksum != source_checksum
        or last.schema_version != SCHEMA_PACKAGE_VERSION
        or imports.has_local_changes(group_key, last.job_id)
    ):
        return None
    return AnnotationRefreshUnchanged(
        source_key=source_key,
        group_key=group_key,
        import_job_id=last.job_id,
        source_checksum=source_checksum,
    )


def _import_result(record: AnnotationImportRecord) -> AnnotationRefreshResult:
    """Build a publication result from a published import record.

    Raises:
        StoredDataError: If the stored rejection report is malformed.
    """
    return AnnotationRefreshResult(
        source_key=record.source_key,
        group_key=record.group_key,
        import_job_id=record.job_id,
        is_cutover=record.is_cutover,
        provenance=SourceProvenance(
            source_type=record.source_type,
            source_locator=record.source_locator,
            source_revision=record.source_revision,
            source_checksum=record.source_checksum,
            fetched_at=record.fetched_at,
        ),
        schema_version=record.schema_version,
        data_rows=record.data_rows,
        annotations_published=record.annotations_staged,
        records_rejected=record.records_rejected,
        annotations_deleted=record.annotations_deleted,
        mode=(
            AnnotationManagementMode.SABE_MANAGED
            if record.is_cutover
            else AnnotationManagementMode.GPAD_IMPORTED
        ),
        rejection_report=STORED_REJECTION_REPORT.load(record.rejection_report),
    )
