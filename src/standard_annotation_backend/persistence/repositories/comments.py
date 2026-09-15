"""Read and write annotation comments in caller-managed transactions."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from standard_annotation_backend.persistence.locks import (
    acquire_global_annotation_write_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AnnotationRecord,
    AnnotationStatus,
)
from standard_annotation_backend.persistence.repositories.annotations import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
)


class CommentNotFoundError(LookupError):
    """Raised when a comment write targets a missing or deleted comment."""


class InvalidCommentError(ValueError):
    """Raised when a comment body is blank."""


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
            The comment, or `None` when no visible comment exists.
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
            InvalidCommentError: If `body` is blank.
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
            InvalidCommentError: If `body` is blank.
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
