"""Stage, check, publish, and clean up GPAD annotation imports.

Every method works in the caller's transaction; the caller commits.
"""

from collections.abc import Collection, Iterator, Sequence
from datetime import UTC, datetime
from itertools import islice
from uuid import UUID

from sqlalchemy import delete, false, func, insert, literal, null, or_, select
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotation_management import (
    AnnotationImportConflictError,
    AnnotationManagementMode,
    AnnotationRefreshResult,
    CutoverRejectedError,
    GroupSabManagedError,
    RejectionReport,
    RejectionReportBuilder,
    unknown_subject_rejection,
)
from standard_annotation_backend.domain.annotations import (
    AnnotationOrigin,
    AnnotationStatus,
    new_annotation_id,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import SourceProvenance
from standard_annotation_backend.gpad.parser import ParsedAnnotation
from standard_annotation_backend.persistence.annotation_data import (
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.locks import (
    acquire_global_annotation_write_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AnnotationDuplicateReferenceRecord,
    AnnotationImportRecord,
    AnnotationMultivaluedFieldValueRecord,
    AnnotationRecord,
    AnnotationStagingDuplicateReferenceRecord,
    AnnotationStagingMultivaluedValueRecord,
    AnnotationStagingRecord,
    AnnotationVersionRecord,
    AuditEventRecord,
    ChangeSetRecord,
    EntityMembershipRecord,
    GroupAnnotationManagementRecord,
    JobRecord,
)

_GPAD_JOB_TYPES = frozenset({JobType.ANNOTATION_REFRESH, JobType.ANNOTATION_CUTOVER})
_SYSTEM_CHANGE_SOURCES = ("ontology_refresh",)
"""Version sources that are system maintenance rather than local edits."""
_INSERT_BATCH_SIZE = 5_000


class AnnotationImportRepository:
    """Read and change GPAD import state in the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_job(self, job_id: UUID) -> JobRecord:
        """Lock a GPAD job's row until the transaction ends and return it.

        Staging, publication, and cleanup for one job therefore run one at a time.

        Raises:
            AnnotationImportConflictError: If the job does not exist or is not a
                GPAD job.
        """
        job = self.session.scalar(
            select(JobRecord)
            .where(JobRecord.job_id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None or job.job_type not in _GPAD_JOB_TYPES:
            raise AnnotationImportConflictError
        return job

    def group_mode(self, group_key: str) -> AnnotationManagementMode:
        """Return a group's mode; a group without a stored mode is `gpad_imported`."""
        mode = self.session.scalar(
            select(GroupAnnotationManagementRecord.mode).where(
                GroupAnnotationManagementRecord.group_key == group_key
            )
        )
        return AnnotationManagementMode(mode or AnnotationManagementMode.GPAD_IMPORTED)

    def sab_managed_groups(self) -> frozenset[str]:
        """Return every group whose annotations are managed by SAB."""
        return frozenset(
            self.session.scalars(
                select(GroupAnnotationManagementRecord.group_key).where(
                    GroupAnnotationManagementRecord.mode
                    == AnnotationManagementMode.SAB_MANAGED.value
                )
            )
        )

    def discard_unpublished(self, job_id: UUID) -> None:
        """Delete a job's staging and its import row unless the import was published.

        A job that runs again after an interruption stages its document afresh.
        """
        self.session.execute(
            delete(AnnotationStagingRecord).where(
                AnnotationStagingRecord.job_id == job_id
            )
        )
        self.session.execute(
            delete(AnnotationImportRecord).where(
                AnnotationImportRecord.job_id == job_id,
                AnnotationImportRecord.published_at.is_(None),
            )
        )

    def active_db_object_ids(self, ids: Collection[str]) -> frozenset[str]:
        """Return which of `ids` are active entity identifiers."""
        if not ids:
            return frozenset()
        return frozenset(
            self.session.scalars(
                select(EntityMembershipRecord.db_object_id).where(
                    EntityMembershipRecord.db_object_id.in_(list(ids))
                )
            )
        )

    def stage_annotations(
        self, job_id: UUID, annotations: Sequence[ParsedAnnotation]
    ) -> None:
        """Store validated annotations for a job until it publishes.

        Each annotation gets a new SAB identifier now, which it keeps when
        published. Its lookup rows, used to filter by multivalued fields and to
        find duplicates, are staged too, computed the same way as for annotations
        created directly in SAB.
        """
        staged: list[dict[str, object]] = []
        multivalued: list[dict[str, object]] = []
        references: list[dict[str, object]] = []
        for parsed in annotations:
            annotation_id = new_annotation_id()
            data = prepare_annotation_for_persistence(parsed.annotation)
            keys = {"job_id": job_id, "annotation_id": annotation_id}
            staged.append(
                {**keys, "line_number": parsed.line_number, **data.column_values()}
            )
            multivalued.extend({**keys, **row} for row in data.multivalued_rows())
            references.extend({**keys, **row} for row in data.reference_rows())
        for model, rows in (
            (AnnotationStagingRecord, staged),
            (AnnotationStagingMultivaluedValueRecord, multivalued),
            (AnnotationStagingDuplicateReferenceRecord, references),
        ):
            for batch in _batches(rows):
                self.session.execute(insert(model), batch)

    def record_staged(
        self,
        *,
        job_id: UUID,
        source_key: str,
        group_key: str,
        is_cutover: bool,
        provenance: SourceProvenance,
        source_metadata: dict[str, object],
        data_rows: int,
        annotations_staged: int,
        report: RejectionReport,
    ) -> AnnotationImportRecord:
        """Record a job's staged, not yet published, import and return the row."""
        record = AnnotationImportRecord(
            job_id=job_id,
            source_key=source_key,
            group_key=group_key,
            is_cutover=is_cutover,
            source_type=provenance.source_type,
            source_locator=provenance.source_locator,
            source_revision=provenance.source_revision,
            source_checksum=provenance.source_checksum,
            fetched_at=provenance.fetched_at,
            source_metadata=source_metadata,
            data_rows=data_rows,
            annotations_staged=annotations_staged,
            records_rejected=report.issue_count,
            rejection_report=report.to_json(),
            staged_at=datetime.now(UTC),
        )
        self.session.add(record)
        self.session.flush([record])
        return record

    def published_result(self, job_id: UUID) -> AnnotationRefreshResult | None:
        """Return a job's publication result, or `None` if it has not published."""
        record = self.session.get(AnnotationImportRecord, job_id)
        if record is None or record.published_at is None:
            return None
        return _result(record)

    def last_import(self, group_key: str) -> AnnotationImportRecord | None:
        """Return the group's most recently published import, if any."""
        return self.session.scalar(
            select(AnnotationImportRecord)
            .join(
                GroupAnnotationManagementRecord,
                GroupAnnotationManagementRecord.last_import_job_id
                == AnnotationImportRecord.job_id,
            )
            .where(GroupAnnotationManagementRecord.group_key == group_key)
        )

    def has_local_changes(self, group_key: str, import_job_id: UUID) -> bool:
        """Report whether the group has changed since `import_job_id` published.

        A local change is an annotation that the import did not publish, an edit
        or soft delete, a comment, or a change set in any state. SAB's automatic
        ontology term replacements are not local changes. This checks current
        rows rather than timestamps, because a write's `created_at` is its
        transaction's start time, which can precede a publication it commits after.
        """
        group_annotations = select(AnnotationRecord.annotation_id).where(
            AnnotationRecord.owning_group_id == group_key
        )
        # Each query selects rows that show a local change; one match is enough.
        local_change_queries = (
            # Annotations the import did not publish: created in SAB directly or
            # through an accepted change set, or left from an earlier import.
            group_annotations.where(
                or_(
                    AnnotationRecord.record_origin != AnnotationOrigin.IMPORT,
                    AnnotationRecord.source_import_job_id.is_distinct_from(
                        import_job_id
                    ),
                )
            ),
            # Edits and soft deletes: imported annotations start at version 1,
            # so a later version means a change, unless SAB made it itself
            # (such as an ontology term replacement).
            select(AnnotationVersionRecord.annotation_id).where(
                AnnotationVersionRecord.annotation_id.in_(group_annotations),
                AnnotationVersionRecord.version > 1,
                AnnotationVersionRecord.change_source.not_in(_SYSTEM_CHANGE_SOURCES),
            ),
            # Comments on the group's annotations, including deleted ones. A
            # comment does not add an annotation version.
            select(AnnotationCommentRecord.comment_id).where(
                AnnotationCommentRecord.annotation_id.in_(group_annotations)
            ),
            # Change sets for the group in any state, including pending create
            # proposals, which have no annotation yet.
            select(ChangeSetRecord.change_set_id).where(
                ChangeSetRecord.owning_group_id == group_key
            ),
        )
        return any(
            self.session.scalar(query.limit(1)) is not None
            for query in local_change_queries
        )

    def cleanup_terminal_staging(self, job_id: UUID) -> None:
        """Delete a finished job's staging and its unpublished import row.

        Staging for a queued or running job is kept. Published import rows are
        kept as provenance.

        Raises:
            AnnotationImportConflictError: If the job does not exist or is not a
                GPAD job.
        """
        job = self.lock_job(job_id)
        if job.status in {"failed", "succeeded"}:
            self.discard_unpublished(job_id)

    def publish(self, job_id: UUID, *, actor_id: str) -> AnnotationRefreshResult:
        """Replace the job's group's annotations with its staged annotations.

        For this group only, deletes the annotations with their versions,
        comments, and lookup rows, the change sets, and the audit events of both.
        Then inserts the staged annotations as version 1 and marks the import
        published. A cutover also moves the group to `sab_managed`.

        Takes the global annotation-write lock in exclusive mode, so every
        annotation write in SAB waits until the caller's transaction ends.
        Because the replacement happens in one transaction, readers see either
        the group's old annotations or the new ones, never a mix.

        Returns:
            The publication result. If the job already published, its stored
            result is returned and nothing changes.

        Raises:
            AnnotationImportConflictError: If the job does not exist, is not a
                GPAD job, or has no staged import.
            GroupSabManagedError: If the group is already SAB-managed.
            CutoverRejectedError: If a cutover's staged subject is no longer an
                active entity.
        """
        self.lock_job(job_id)
        record = self.session.get(
            AnnotationImportRecord, job_id, populate_existing=True
        )
        if record is None:
            raise AnnotationImportConflictError
        if record.published_at is not None:
            return _result(record)
        acquire_global_annotation_write_lock(self.session, exclusive=True)
        state = self._lock_group(record.group_key)
        if state.mode == AnnotationManagementMode.SAB_MANAGED.value:
            raise GroupSabManagedError(record.group_key)
        if record.is_cutover:
            self._require_staged_subjects(job_id)
        deleted = self._delete_group_data(record.group_key)
        change_source = (
            "annotation_cutover" if record.is_cutover else "annotation_refresh"
        )
        self._copy_staging(job_id, record.group_key, actor_id, change_source)
        now = datetime.now(UTC)
        record.published_at = now
        record.annotations_deleted = deleted
        state.last_import_job_id = job_id
        if record.is_cutover:
            state.mode = AnnotationManagementMode.SAB_MANAGED.value
            state.transitioned_at = now
            state.transition_job_id = job_id
        self.session.execute(
            delete(AnnotationStagingRecord).where(
                AnnotationStagingRecord.job_id == job_id
            )
        )
        self.session.flush()
        return _result(record)

    def _lock_group(self, group_key: str) -> GroupAnnotationManagementRecord:
        """Create the group's row if needed and lock it until the transaction ends."""
        self.session.execute(
            pg_insert(GroupAnnotationManagementRecord)
            .values(group_key=group_key)
            .on_conflict_do_nothing(index_elements=["group_key"])
        )
        state = self.session.scalar(
            select(GroupAnnotationManagementRecord)
            .where(GroupAnnotationManagementRecord.group_key == group_key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if state is None:
            raise AnnotationImportConflictError
        return state

    def _require_staged_subjects(self, job_id: UUID) -> None:
        """Require every staged subject to stay an active entity until commit.

        Each subject's `entity_membership` row is locked with PostgreSQL's
        `FOR KEY SHARE` row lock, so an entity publication cannot remove the
        subject before this transaction commits. Entity publication replaces a
        source's rows by deleting and reinserting them, so a row this query waited
        on can disappear while an equal row appears. The query is repeated until
        every subject that is still present is locked, as in
        `EntityRepository.require_active`.

        A staged subject is accepted only if its row is among those locked. A
        subject that becomes active after the locks are taken is rejected, because
        nothing stops its row from being removed again before commit.

        Raises:
            CutoverRejectedError: Listing staged annotations whose subject is no
                longer active.
        """
        staged_ids = (
            select(AnnotationStagingRecord.db_object_id)
            .where(AnnotationStagingRecord.job_id == job_id)
            .distinct()
        )
        membership = EntityMembershipRecord
        present_count = (
            select(func.count())
            .select_from(membership)
            .where(membership.db_object_id.in_(staged_ids))
            .scalar_subquery()
        )
        while True:
            locked = set(
                self.session.scalars(
                    select(membership.db_object_id)
                    .where(membership.db_object_id.in_(staged_ids))
                    .with_for_update(read=True, key_share=True)
                )
            )
            if len(locked) >= (self.session.scalar(select(present_count)) or 0):
                break
        builder = RejectionReportBuilder()
        staged = self.session.execute(
            select(
                AnnotationStagingRecord.line_number,
                AnnotationStagingRecord.db_object_id,
            )
            .where(AnnotationStagingRecord.job_id == job_id)
            .order_by(AnnotationStagingRecord.line_number)
        )
        for line_number, db_object_id in staged:
            if db_object_id not in locked:
                builder.add(unknown_subject_rejection(line_number, db_object_id))
        report = builder.build()
        if report.issue_count:
            raise CutoverRejectedError(report)

    def _delete_group_data(self, group_key: str) -> int:
        """Delete a group's annotations and dependents; return annotations deleted."""
        group_annotations = select(AnnotationRecord.annotation_id).where(
            AnnotationRecord.owning_group_id == group_key
        )
        group_change_sets = select(ChangeSetRecord.change_set_id).where(
            ChangeSetRecord.owning_group_id == group_key
        )
        self.session.execute(
            delete(AuditEventRecord)
            .where(
                or_(
                    AuditEventRecord.annotation_id.in_(group_annotations),
                    AuditEventRecord.change_set_id.in_(group_change_sets),
                )
            )
            .execution_options(synchronize_session=False)
        )
        self.session.execute(
            delete(ChangeSetRecord)
            .where(ChangeSetRecord.owning_group_id == group_key)
            .execution_options(synchronize_session=False)
        )
        deleted_ids = self.session.scalars(
            delete(AnnotationRecord)
            .where(AnnotationRecord.owning_group_id == group_key)
            .returning(AnnotationRecord.annotation_id)
            .execution_options(synchronize_session=False)
        )
        return len(deleted_ids.all())

    def _copy_staging(
        self, job_id: UUID, group_key: str, actor_id: str, change_source: str
    ) -> None:
        """Insert a job's staged annotations, versions, and lookup rows."""
        staging = AnnotationStagingRecord
        job = literal(job_id, PostgreSQLUUID(as_uuid=True))
        self.session.execute(
            insert(AnnotationRecord).from_select(
                [
                    "annotation_id",
                    "annotation_data",
                    "current_version",
                    "status",
                    "deleted_at",
                    "owning_group_id",
                    "record_origin",
                    "source_import_job_id",
                    "duplicate_base_signature",
                    "db_object_id",
                    "negation",
                    "relation",
                    "ontology_class_id",
                    "evidence_type",
                    "annotation_date",
                    "assigned_by",
                ],
                select(
                    staging.annotation_id,
                    staging.annotation_data,
                    literal(1),
                    literal(AnnotationStatus.ACTIVE.value),
                    null(),
                    literal(group_key),
                    literal(AnnotationOrigin.IMPORT.value),
                    job,
                    staging.duplicate_base_signature,
                    staging.db_object_id,
                    staging.negation,
                    staging.relation,
                    staging.ontology_class_id,
                    staging.evidence_type,
                    staging.annotation_date,
                    staging.assigned_by,
                ).where(staging.job_id == job_id),
            )
        )
        self.session.execute(
            insert(AnnotationVersionRecord).from_select(
                [
                    "annotation_id",
                    "version",
                    "annotation_data",
                    "is_deleted",
                    "actor_id",
                    "change_source",
                ],
                select(
                    staging.annotation_id,
                    literal(1),
                    staging.annotation_data,
                    false(),
                    literal(actor_id),
                    literal(change_source),
                ).where(staging.job_id == job_id),
            )
        )
        values = AnnotationStagingMultivaluedValueRecord
        self.session.execute(
            insert(AnnotationMultivaluedFieldValueRecord).from_select(
                ["annotation_id", "field_name", "field_value"],
                select(
                    values.annotation_id, values.field_name, values.field_value
                ).where(values.job_id == job_id),
            )
        )
        references = AnnotationStagingDuplicateReferenceRecord
        self.session.execute(
            insert(AnnotationDuplicateReferenceRecord).from_select(
                ["annotation_id", "canonical_reference", "duplicate_base_signature"],
                select(
                    references.annotation_id,
                    references.canonical_reference,
                    references.duplicate_base_signature,
                ).where(references.job_id == job_id),
            )
        )


def _result(record: AnnotationImportRecord) -> AnnotationRefreshResult:
    """Build a publication result from a published import row."""
    if record.annotations_deleted is None:
        raise AnnotationImportConflictError
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
        data_rows=record.data_rows,
        annotations_published=record.annotations_staged,
        records_rejected=record.records_rejected,
        annotations_deleted=record.annotations_deleted,
        mode=(
            AnnotationManagementMode.SAB_MANAGED
            if record.is_cutover
            else AnnotationManagementMode.GPAD_IMPORTED
        ),
        rejection_report=RejectionReport.from_json(record.rejection_report),
    )


def _batches(rows: Sequence[dict[str, object]]) -> Iterator[list[dict[str, object]]]:
    """Split rows into lists of at most 5,000 for batched inserts."""
    iterator = iter(rows)
    while batch := list(islice(iterator, _INSERT_BATCH_SIZE)):
        yield batch
