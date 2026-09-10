"""Read and write annotations and comments with SQLAlchemy sessions."""

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotations import Annotation, new_annotation_id
from standard_annotation_backend.persistence.annotation_data import (
    AnnotationPersistenceData,
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.locks import (
    acquire_global_annotation_write_lock,
    acquire_signature_locks,
)
from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AnnotationDuplicateReferenceRecord,
    AnnotationMultivaluedFieldValueRecord,
    AnnotationOrigin,
    AnnotationRecord,
    AnnotationStatus,
    AnnotationVersionRecord,
)


class AnnotationNotFoundError(LookupError):
    """Raised when an annotation write targets an unknown identifier."""


class AnnotationDeletedError(RuntimeError):
    """Raised when an annotation write targets a soft-deleted annotation."""


class InvalidAnnotationProvenanceError(ValueError):
    """Raised when annotation provenance is unsupported or inconsistent."""


class CommentNotFoundError(LookupError):
    """Raised when a comment write targets a missing or deleted comment."""


class InvalidCommentError(ValueError):
    """Raised when a comment body is blank."""


class AnnotationRepository:
    """Read and write annotations in a caller-managed transaction.

    The repository flushes changes so database errors are raised promptly, but
    the caller remains responsible for committing or rolling back the session.

    Args:
        session: Session used for every query and write.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self,
        annotation_id: UUID,
        *,
        include_deleted: bool = False,
    ) -> AnnotationRecord | None:
        """Get an annotation's current database record.

        Args:
            annotation_id: Identifier of the annotation to find.
            include_deleted: Whether a deleted annotation may be returned.

        Returns:
            The current record, or `None` when no visible record exists.
        """
        statement = select(AnnotationRecord).where(
            AnnotationRecord.annotation_id == annotation_id
        )
        if not include_deleted:
            statement = statement.where(
                AnnotationRecord.status == AnnotationStatus.ACTIVE.value
            )
        return self.session.scalar(statement)

    def list_versions(
        self,
        annotation_id: UUID,
    ) -> tuple[AnnotationVersionRecord, ...]:
        """List the saved versions of an annotation.

        Args:
            annotation_id: Identifier of the annotation whose history is needed.

        Returns:
            Saved versions ordered from oldest to newest.
        """
        statement = (
            select(AnnotationVersionRecord)
            .where(AnnotationVersionRecord.annotation_id == annotation_id)
            .order_by(AnnotationVersionRecord.version)
        )
        return tuple(self.session.scalars(statement))

    def find_duplicate_peer_ids(
        self,
        duplicate_base_signature: str,
        canonical_references: Iterable[str],
        *,
        exclude_annotation_id: UUID | None = None,
    ) -> tuple[UUID, ...]:
        """Find active annotations that may duplicate a candidate annotation.

        Args:
            duplicate_base_signature: Signature for the candidate's normalized
                non-reference fields.
            canonical_references: Normalized references from the candidate.
            exclude_annotation_id: Annotation to leave out of the results, such
                as the annotation currently being updated.

        Returns:
            Matching annotation identifiers in stable order.
        """
        references = tuple(set(canonical_references))
        if not references:
            return ()

        statement = (
            select(AnnotationDuplicateReferenceRecord.annotation_id)
            .join(
                AnnotationRecord,
                AnnotationRecord.annotation_id
                == AnnotationDuplicateReferenceRecord.annotation_id,
            )
            .where(
                AnnotationRecord.status == AnnotationStatus.ACTIVE.value,
                AnnotationDuplicateReferenceRecord.duplicate_base_signature
                == duplicate_base_signature,
                AnnotationDuplicateReferenceRecord.canonical_reference.in_(references),
            )
            .distinct()
            .order_by(AnnotationDuplicateReferenceRecord.annotation_id)
        )
        if exclude_annotation_id is not None:
            statement = statement.where(
                AnnotationDuplicateReferenceRecord.annotation_id
                != exclude_annotation_id
            )
        return tuple(self.session.scalars(statement))

    def create(
        self,
        *,
        annotation: Annotation,
        actor_id: str,
        change_source: str,
        owning_group_id: str,
        record_origin: str | AnnotationOrigin,
        source_import_job_id: UUID | None = None,
        annotation_id: UUID | None = None,
    ) -> AnnotationRecord:
        """Store a new annotation and its first version.

        Args:
            annotation: Validated annotation to store.
            actor_id: Identifier for the person or process making the change.
            change_source: Name of the workflow that made the change.
            owning_group_id: Group responsible for the annotation.
            record_origin: Whether the annotation was created directly or
                imported.
            source_import_job_id: Import job that supplied the annotation.
            annotation_id: Identifier to use instead of generating one.

        Returns:
            The new current annotation record.

        Raises:
            InvalidAnnotationProvenanceError: If the origin and import job do not
                describe a valid direct or imported record.
            TypeError: If `annotation` is not a validated `Annotation`.
        """
        origin = (
            record_origin.value
            if isinstance(record_origin, AnnotationOrigin)
            else record_origin
        )
        self._validate_provenance(origin, source_import_job_id)
        persistence_data = prepare_annotation_for_persistence(annotation)

        acquire_global_annotation_write_lock(self.session)
        acquire_signature_locks(
            self.session,
            [persistence_data.duplicate_base_signature],
        )

        resolved_annotation_id = annotation_id or new_annotation_id()
        record = AnnotationRecord(
            annotation_id=resolved_annotation_id,
            annotation_data=persistence_data.annotation_data,
            current_version=1,
            status=AnnotationStatus.ACTIVE.value,
            deleted_at=None,
            owning_group_id=owning_group_id,
            record_origin=origin,
            source_import_job_id=source_import_job_id,
            duplicate_base_signature=persistence_data.duplicate_base_signature,
            db_object_id=persistence_data.db_object_id,
            negation=persistence_data.negation,
            relation=persistence_data.relation,
            ontology_class_id=persistence_data.ontology_class_id,
            evidence_type=persistence_data.evidence_type,
            annotation_date=persistence_data.annotation_date,
            assigned_by=persistence_data.assigned_by,
        )
        self.session.add(record)
        self.session.flush([record])
        self.session.add(
            AnnotationVersionRecord(
                annotation_id=resolved_annotation_id,
                version=1,
                annotation_data=persistence_data.annotation_data,
                is_deleted=False,
                actor_id=actor_id,
                change_source=change_source,
            )
        )
        self._add_derived_values(resolved_annotation_id, persistence_data)
        self.session.flush()
        return record

    def update(
        self,
        annotation_id: UUID,
        annotation: Annotation,
        *,
        actor_id: str,
        change_source: str,
    ) -> AnnotationRecord:
        """Replace an annotation's current data and add a saved version.

        Args:
            annotation_id: Identifier of the annotation to update.
            annotation: Validated replacement annotation.
            actor_id: Identifier for the person or process making the change.
            change_source: Name of the workflow that made the change.

        Returns:
            The updated current annotation record.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has already been deleted.
            TypeError: If `annotation` is not a validated `Annotation`.
        """
        persistence_data = prepare_annotation_for_persistence(annotation)
        acquire_global_annotation_write_lock(self.session)
        record = self._get_annotation_for_change(annotation_id)
        self._raise_if_deleted(record)

        acquire_signature_locks(
            self.session,
            [
                record.duplicate_base_signature,
                persistence_data.duplicate_base_signature,
            ],
        )

        next_version = record.current_version + 1
        now = datetime.now(UTC)
        self._update_current_record(record, persistence_data)
        record.current_version = next_version
        record.updated_at = now

        self.session.add(
            AnnotationVersionRecord(
                annotation_id=annotation_id,
                version=next_version,
                annotation_data=persistence_data.annotation_data,
                is_deleted=False,
                actor_id=actor_id,
                change_source=change_source,
                created_at=now,
            )
        )
        self._delete_derived_values(annotation_id)
        self._add_derived_values(annotation_id, persistence_data)
        self.session.flush()
        return record

    def soft_delete(
        self,
        annotation_id: UUID,
        *,
        actor_id: str,
        change_source: str,
    ) -> AnnotationRecord:
        """Mark an annotation as deleted and save that change as a new version.

        Args:
            annotation_id: Identifier of the annotation to delete.
            actor_id: Identifier for the person or process making the change.
            change_source: Name of the workflow that made the change.

        Returns:
            The annotation record in its deleted state.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has already been deleted.
        """
        acquire_global_annotation_write_lock(self.session)
        record = self._get_annotation_for_change(annotation_id)
        self._raise_if_deleted(record)

        acquire_signature_locks(self.session, [record.duplicate_base_signature])

        next_version = record.current_version + 1
        now = datetime.now(UTC)
        record.current_version = next_version
        record.status = AnnotationStatus.DELETED.value
        record.deleted_at = now
        record.updated_at = now
        self.session.add(
            AnnotationVersionRecord(
                annotation_id=annotation_id,
                version=next_version,
                annotation_data=record.annotation_data,
                is_deleted=True,
                actor_id=actor_id,
                change_source=change_source,
                created_at=now,
            )
        )
        self._delete_derived_values(annotation_id)
        self.session.flush()
        return record

    @staticmethod
    def _validate_provenance(
        record_origin: str,
        source_import_job_id: UUID | None,
    ) -> None:
        try:
            AnnotationOrigin(record_origin)
        except ValueError:
            raise InvalidAnnotationProvenanceError(
                f"{record_origin!r} is not a supported annotation record origin"
            ) from None

        if (
            record_origin == AnnotationOrigin.DIRECT.value
            and source_import_job_id is not None
        ):
            raise InvalidAnnotationProvenanceError(
                "direct annotations cannot have a source import job"
            )
        if (
            record_origin == AnnotationOrigin.IMPORT.value
            and source_import_job_id is None
        ):
            raise InvalidAnnotationProvenanceError(
                "imported annotations require a source import job"
            )

    def _get_annotation_for_change(self, annotation_id: UUID) -> AnnotationRecord:
        """Get the current annotation and prevent concurrent changes to it.

        Args:
            annotation_id: Identifier of the annotation being changed.

        Returns:
            The current annotation record.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
        """
        statement = (
            select(AnnotationRecord)
            .where(AnnotationRecord.annotation_id == annotation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        record = self.session.scalar(statement)
        if record is None:
            raise AnnotationNotFoundError(f"annotation {annotation_id} was not found")
        return record

    @staticmethod
    def _raise_if_deleted(record: AnnotationRecord) -> None:
        if record.status == AnnotationStatus.DELETED.value:
            raise AnnotationDeletedError(
                f"annotation {record.annotation_id} is already deleted"
            )

    @staticmethod
    def _update_current_record(
        record: AnnotationRecord,
        persistence_data: AnnotationPersistenceData,
    ) -> None:
        record.annotation_data = persistence_data.annotation_data
        record.duplicate_base_signature = persistence_data.duplicate_base_signature
        record.db_object_id = persistence_data.db_object_id
        record.negation = persistence_data.negation
        record.relation = persistence_data.relation
        record.ontology_class_id = persistence_data.ontology_class_id
        record.evidence_type = persistence_data.evidence_type
        record.annotation_date = persistence_data.annotation_date
        record.assigned_by = persistence_data.assigned_by

    def _delete_derived_values(self, annotation_id: UUID) -> None:
        self.session.execute(
            delete(AnnotationMultivaluedFieldValueRecord).where(
                AnnotationMultivaluedFieldValueRecord.annotation_id == annotation_id
            )
        )
        self.session.execute(
            delete(AnnotationDuplicateReferenceRecord).where(
                AnnotationDuplicateReferenceRecord.annotation_id == annotation_id
            )
        )

    def _add_derived_values(
        self,
        annotation_id: UUID,
        persistence_data: AnnotationPersistenceData,
    ) -> None:
        self.session.add_all(
            AnnotationMultivaluedFieldValueRecord(
                annotation_id=annotation_id,
                field_name=value.field_name,
                field_value=value.field_value,
            )
            for value in persistence_data.multivalued_field_values
        )
        self.session.add_all(
            AnnotationDuplicateReferenceRecord(
                annotation_id=annotation_id,
                canonical_reference=reference,
                duplicate_base_signature=persistence_data.duplicate_base_signature,
            )
            for reference in persistence_data.canonical_references
        )


class AnnotationCommentRepository:
    """Read and write comments in a caller-managed transaction.

    Each comment stays attached to the annotation version that was current when
    the comment was created.

    Args:
        session: Session used for every query and write.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self,
        comment_id: UUID,
        *,
        include_deleted: bool = False,
    ) -> AnnotationCommentRecord | None:
        """Get a comment by identifier.

        Args:
            comment_id: Identifier of the comment to find.
            include_deleted: Whether a deleted comment may be returned.

        Returns:
            The comment, or ``None`` when no visible comment exists.
        """
        statement = select(AnnotationCommentRecord).where(
            AnnotationCommentRecord.comment_id == comment_id
        )
        if not include_deleted:
            statement = statement.where(AnnotationCommentRecord.deleted_at.is_(None))
        return self.session.scalar(statement)

    def list(
        self,
        annotation_id: UUID,
        *,
        include_deleted: bool = False,
    ) -> tuple[AnnotationCommentRecord, ...]:
        """List comments attached to an annotation.

        Args:
            annotation_id: Identifier of the annotation to list comments for.
            include_deleted: Whether deleted comments should be included.

        Returns:
            Comments ordered by creation time and identifier.
        """
        statement = select(AnnotationCommentRecord).where(
            AnnotationCommentRecord.annotation_id == annotation_id
        )
        if not include_deleted:
            statement = statement.where(AnnotationCommentRecord.deleted_at.is_(None))
        statement = statement.order_by(
            AnnotationCommentRecord.created_at,
            AnnotationCommentRecord.comment_id,
        )
        return tuple(self.session.scalars(statement))

    def create(
        self,
        annotation_id: UUID,
        *,
        body: str,
        created_by: str,
        comment_id: UUID | None = None,
    ) -> AnnotationCommentRecord:
        """Create a comment on an annotation's current version.

        Args:
            annotation_id: Identifier of the annotation being discussed.
            body: Nonblank comment text.
            created_by: Identifier for the comment author.
            comment_id: Identifier to use instead of generating one.

        Returns:
            The new comment record.

        Raises:
            InvalidCommentError: If ``body`` is blank.
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has been deleted.
        """
        self._validate_body(body)
        acquire_global_annotation_write_lock(self.session)
        annotation = self._get_active_annotation_for_comment(annotation_id)

        now = datetime.now(UTC)
        comment = AnnotationCommentRecord(
            comment_id=comment_id or uuid4(),
            annotation_id=annotation_id,
            annotation_version=annotation.current_version,
            body=body,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        self.session.add(comment)
        self.session.flush([comment])
        return comment

    def edit(self, comment_id: UUID, *, body: str) -> AnnotationCommentRecord:
        """Change the text of an existing comment.

        Args:
            comment_id: Identifier of the comment to edit.
            body: New nonblank comment text.

        Returns:
            The updated comment record.

        Raises:
            InvalidCommentError: If ``body`` is blank.
            CommentNotFoundError: If the comment is missing or deleted.
        """
        self._validate_body(body)
        acquire_global_annotation_write_lock(self.session)
        comment = self._get_active_comment_for_change(comment_id)
        comment.body = body
        comment.updated_at = datetime.now(UTC)
        self.session.flush([comment])
        return comment

    def soft_delete(self, comment_id: UUID) -> AnnotationCommentRecord:
        """Mark a comment as deleted without removing its row.

        Args:
            comment_id: Identifier of the comment to delete.

        Returns:
            The comment record in its deleted state.

        Raises:
            CommentNotFoundError: If the comment is missing or already deleted.
        """
        acquire_global_annotation_write_lock(self.session)
        comment = self._get_active_comment_for_change(comment_id)
        now = datetime.now(UTC)
        comment.updated_at = now
        comment.deleted_at = now
        self.session.flush([comment])
        return comment

    def _get_active_annotation_for_comment(
        self,
        annotation_id: UUID,
    ) -> AnnotationRecord:
        """Get the active annotation version that a new comment will reference.

        Other transactions cannot change the annotation until this transaction ends.

        Args:
            annotation_id: Identifier of the annotation being discussed.

        Returns:
            The active annotation record with its current version protected.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has been deleted.
        """
        statement = (
            select(AnnotationRecord)
            .where(AnnotationRecord.annotation_id == annotation_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        annotation = self.session.scalar(statement)
        if annotation is None:
            raise AnnotationNotFoundError(f"annotation {annotation_id} was not found")
        if annotation.status == AnnotationStatus.DELETED.value:
            raise AnnotationDeletedError(
                f"annotation {annotation.annotation_id} is already deleted"
            )
        return annotation

    def _get_active_comment_for_change(
        self,
        comment_id: UUID,
    ) -> AnnotationCommentRecord:
        """Get an active comment and prevent concurrent changes to it.

        Args:
            comment_id: Identifier of the comment being changed.

        Returns:
            The active comment record.

        Raises:
            CommentNotFoundError: If the comment is missing or deleted.
        """
        statement = (
            select(AnnotationCommentRecord)
            .where(
                AnnotationCommentRecord.comment_id == comment_id,
                AnnotationCommentRecord.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        comment = self.session.scalar(statement)
        if comment is None:
            raise CommentNotFoundError(
                f"comment {comment_id} was not found or is deleted"
            )
        return comment

    @staticmethod
    def _validate_body(body: str) -> None:
        if not body.strip():
            raise InvalidCommentError("comment body must not be blank")
