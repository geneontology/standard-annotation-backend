"""Stage and publish GPAD annotation imports in the caller's transaction.

Staged annotations are held in temporary tables (`persistence.annotation_staging`)
that exist only in the caller's transaction. The caller commits.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched
from uuid import UUID

from go_standard_annotation_schema.io import Record
from sqlalchemy import delete, false, insert, literal, null, or_, select
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotation_management import (
    AnnotationImportConflictError,
    AnnotationManagementMode,
    GroupSabeManagedError,
)
from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    AnnotationStatus,
    ChangeSource,
    new_annotation_id,
)
from standard_annotation_backend.domain.refresh import (
    STORED_REJECTION_REPORT,
    RejectionReport,
    SourceProvenance,
)
from standard_annotation_backend.persistence.annotation_data import (
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.annotation_staging import (
    STAGED_ANNOTATIONS,
    STAGED_DUPLICATE_REFERENCES,
    STAGED_MULTIVALUED_VALUES,
    create_staging_tables,
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
    AnnotationVersionRecord,
    AuditEventRecord,
    ChangeSetRecord,
    GroupAnnotationManagementRecord,
)

_SYSTEM_CHANGE_SOURCES = (ChangeSource.TERM_REPLACEMENT,)
"""Version sources that are system maintenance rather than local edits."""
_INSERT_BATCH_SIZE = 5_000


@dataclass(frozen=True, slots=True)
class StagedImport:
    """Describe a GPAD file whose valid annotations a job has staged.

    Attributes:
        job_id: The GPAD job.
        source_key: The configured GPAD source.
        group_key: The group whose annotations the file replaces.
        is_cutover: Whether the job is a cutover.
        provenance: Where the file came from.
        source_metadata: The file's header values.
        data_rows: Data rows in the file.
        annotations_staged: Annotations staged for publication.
        report: The records that were rejected.
        schema_version: The `go-standard-annotation-schema` version the rows
            were checked with.
        staged_at: When staging finished.
    """

    job_id: UUID
    source_key: str
    group_key: str
    is_cutover: bool
    provenance: SourceProvenance
    source_metadata: dict[str, object]
    data_rows: int
    annotations_staged: int
    report: RejectionReport
    schema_version: str
    staged_at: datetime


class AnnotationImportRepository:
    """Read and change GPAD import state in the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def group_mode(self, group_key: str) -> AnnotationManagementMode:
        """Return a group's mode; a group without a stored mode is `gpad_imported`."""
        mode = self.session.scalar(
            select(GroupAnnotationManagementRecord.mode).where(
                GroupAnnotationManagementRecord.group_key == group_key
            )
        )
        return AnnotationManagementMode(mode or AnnotationManagementMode.GPAD_IMPORTED)

    def sabe_managed_groups(self) -> frozenset[str]:
        """Return every group whose annotations are managed by SABE."""
        return frozenset(
            self.session.scalars(
                select(GroupAnnotationManagementRecord.group_key).where(
                    GroupAnnotationManagementRecord.mode
                    == AnnotationManagementMode.SABE_MANAGED.value
                )
            )
        )

    def begin_staging(self) -> None:
        """Create the staging tables for one import.

        Call it once per import, before `stage_annotations`, in the transaction
        that will publish. The tables disappear when that transaction ends, whether
        it commits or rolls back.
        """
        create_staging_tables(self.session.connection())

    def stage_annotations(self, annotations: Sequence[Record[Annotation]]) -> None:
        """Store validated annotations until the import publishes.

        Each annotation gets a new SABE identifier now, which it keeps when
        published. Its lookup rows, used to filter by multivalued fields and to
        find duplicates, are staged too, computed the same way as for annotations
        created directly in SABE. `begin_staging` must have been called in this
        transaction.
        """
        staged: list[dict[str, object]] = []
        multivalued: list[dict[str, object]] = []
        references: list[dict[str, object]] = []
        for parsed in annotations:
            annotation_id = new_annotation_id()
            data = prepare_annotation_for_persistence(parsed.item)
            key = {"annotation_id": annotation_id}
            staged.append(
                {**key, "line_number": parsed.line_number, **data.column_values()}
            )
            multivalued.extend({**key, **row} for row in data.multivalued_rows())
            references.extend({**key, **row} for row in data.reference_rows())
        for table, rows in (
            (STAGED_ANNOTATIONS, staged),
            (STAGED_MULTIVALUED_VALUES, multivalued),
            (STAGED_DUPLICATE_REFERENCES, references),
        ):
            for batch in batched(rows, _INSERT_BATCH_SIZE, strict=False):
                self.session.execute(insert(table), batch)

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
        or soft delete, a comment, or a change set in any state. Versions written by
        the term replacement job are not local changes. This checks current
        rows rather than timestamps, because a write's `created_at` is its
        transaction's start time, which can precede a publication it commits after.
        """
        group_annotations = select(AnnotationRecord.annotation_id).where(
            AnnotationRecord.owning_group_id == group_key
        )
        # Each query selects rows that show a local change; one match is enough.
        local_change_queries = (
            # Annotations the import did not publish: created in SABE directly or
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
            # so a later version means a change, unless SABE made it itself
            # (such as a term replacement).
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

    def publish(self, staged: StagedImport, *, actor_id: str) -> AnnotationImportRecord:
        """Replace the group's annotations with the staged annotations.

        For this group only, deletes the annotations with their versions,
        comments, and lookup rows, the change sets, and the audit events of both.
        Then inserts the staged annotations as version 1 and records the import. A
        cutover also moves the group to `sabe_managed`. The staging tables are
        dropped when the caller's transaction ends.

        Takes the global annotation-write lock in exclusive mode, so every
        annotation write in SABE waits until the caller's transaction ends.
        Because the replacement happens in one transaction, readers see either
        the group's old annotations or the new ones, never a mix.

        Returns:
            The import record.

        Raises:
            GroupSabeManagedError: If the group is already SABE-managed.
        """
        acquire_global_annotation_write_lock(self.session, exclusive=True)
        state = self._lock_group(staged.group_key)
        if state.mode == AnnotationManagementMode.SABE_MANAGED.value:
            raise GroupSabeManagedError(staged.group_key)
        deleted = self._delete_group_data(staged.group_key)
        change_source = (
            ChangeSource.ANNOTATION_CUTOVER
            if staged.is_cutover
            else ChangeSource.ANNOTATION_REFRESH
        )
        self._copy_staging(staged.job_id, staged.group_key, actor_id, change_source)
        now = datetime.now(UTC)
        record = AnnotationImportRecord(
            job_id=staged.job_id,
            source_key=staged.source_key,
            group_key=staged.group_key,
            is_cutover=staged.is_cutover,
            source_type=staged.provenance.source_type,
            source_locator=staged.provenance.source_locator,
            source_revision=staged.provenance.source_revision,
            source_checksum=staged.provenance.source_checksum,
            fetched_at=staged.provenance.fetched_at,
            source_metadata=staged.source_metadata,
            data_rows=staged.data_rows,
            annotations_staged=staged.annotations_staged,
            records_rejected=staged.report.issue_count,
            rejection_report=STORED_REJECTION_REPORT.dump(staged.report),
            schema_version=staged.schema_version,
            staged_at=staged.staged_at,
            published_at=now,
            annotations_deleted=deleted,
        )
        self.session.add(record)
        self.session.flush([record])
        state.last_import_job_id = staged.job_id
        if staged.is_cutover:
            state.mode = AnnotationManagementMode.SABE_MANAGED.value
            state.transitioned_at = now
            state.transition_job_id = staged.job_id
        self.session.flush()
        return record

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
        self, job_id: UUID, group_key: str, actor_id: str, change_source: ChangeSource
    ) -> None:
        """Insert the staged annotations, their versions, and their lookup rows."""
        staging = STAGED_ANNOTATIONS.c
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
                ),
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
                    literal(change_source.value),
                ),
            )
        )
        values = STAGED_MULTIVALUED_VALUES.c
        self.session.execute(
            insert(AnnotationMultivaluedFieldValueRecord).from_select(
                ["annotation_id", "field_name", "field_value"],
                select(values.annotation_id, values.field_name, values.field_value),
            )
        )
        references = STAGED_DUPLICATE_REFERENCES.c
        self.session.execute(
            insert(AnnotationDuplicateReferenceRecord).from_select(
                ["annotation_id", "canonical_reference", "duplicate_base_signature"],
                select(
                    references.annotation_id,
                    references.canonical_reference,
                    references.duplicate_base_signature,
                ),
            )
        )
