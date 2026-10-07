"""Read and write annotation comments in caller-managed transactions."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotations import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
    AnnotationStatus,
)
from standard_annotation_backend.persistence.locks import (
    acquire_global_annotation_write_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AnnotationRecord,
)
from standard_annotation_backend.persistence.repositories.pagination import (
    Page,
    load_page,
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

    def get_for_annotation(
        self,
        annotation_id: UUID,
        comment_id: UUID,
    ) -> AnnotationCommentRecord | None:
        """Get a comment only when it belongs to the specified annotation.

        Deleted comments are never returned.

        Args:
            annotation_id: Identifier of the expected parent annotation.
            comment_id: Identifier of the comment to find.

        Returns:
            The matching comment, or `None` when no active match exists.
        """
        statement = select(AnnotationCommentRecord).where(
            AnnotationCommentRecord.annotation_id == annotation_id,
            AnnotationCommentRecord.comment_id == comment_id,
            AnnotationCommentRecord.deleted_at.is_(None),
        )
        return self.session.scalar(statement)

    def list(
        self,
        annotation_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> Page[AnnotationCommentRecord]:
        """List one page of active comments attached to an annotation.

        Deleted comments are excluded.

        Args:
            annotation_id: Identifier of the annotation to list comments for.
            limit: Maximum number of comments to return.
            offset: Number of matching comments to skip.

        Returns:
            Selected comments and the total number of matches.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has been deleted.
        """
        self._lock_active_annotation_for_comment(annotation_id)
        statement = select(AnnotationCommentRecord).where(
            AnnotationCommentRecord.annotation_id == annotation_id,
            AnnotationCommentRecord.deleted_at.is_(None),
        )
        return load_page(
            self.session,
            statement,
            record_type=AnnotationCommentRecord,
            order_by=(
                AnnotationCommentRecord.created_at,
                AnnotationCommentRecord.comment_id,
            ),
            limit=limit,
            offset=offset,
        )

    def create(
        self,
        annotation_id: UUID,
        *,
        body: str,
        created_by: str,
    ) -> AnnotationCommentRecord:
        """Create a comment on an annotation's current version.

        Args:
            annotation_id: Identifier of the annotation being discussed.
            body: Nonblank comment text.
            created_by: Identifier for the comment author.

        Returns:
            The new comment record.

        Raises:
            InvalidCommentError: If `body` is blank.
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has been deleted.
        """
        self._validate_body(body)
        acquire_global_annotation_write_lock(self.session)
        annotation = self._lock_active_annotation_for_comment(annotation_id)

        now = datetime.now(UTC)
        comment = AnnotationCommentRecord(
            comment_id=uuid4(),
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

    def edit(
        self,
        annotation_id: UUID,
        comment_id: UUID,
        *,
        body: str,
    ) -> AnnotationCommentRecord:
        """Change the text of an existing comment.

        Args:
            annotation_id: Identifier of the comment's parent annotation.
            comment_id: Identifier of the comment to edit.
            body: New nonblank comment text.

        Returns:
            The updated comment record.

        Raises:
            InvalidCommentError: If `body` is blank.
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has been deleted.
            CommentNotFoundError: If the comment is missing or deleted.
        """
        self._validate_body(body)
        acquire_global_annotation_write_lock(self.session)
        self._lock_active_annotation_for_comment(annotation_id)
        comment = self._get_active_comment_for_change(annotation_id, comment_id)
        comment.body = body
        comment.updated_at = datetime.now(UTC)
        self.session.flush([comment])
        return comment

    def soft_delete(
        self,
        annotation_id: UUID,
        comment_id: UUID,
    ) -> AnnotationCommentRecord:
        """Mark a comment as deleted without removing its row.

        Args:
            annotation_id: Identifier of the comment's parent annotation.
            comment_id: Identifier of the comment to delete.

        Returns:
            The comment record in its deleted state.

        Raises:
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation has been deleted.
            CommentNotFoundError: If the comment is missing or already deleted.
        """
        acquire_global_annotation_write_lock(self.session)
        self._lock_active_annotation_for_comment(annotation_id)
        comment = self._get_active_comment_for_change(annotation_id, comment_id)
        now = datetime.now(UTC)
        comment.updated_at = now
        comment.deleted_at = now
        self.session.flush([comment])
        return comment

    def _lock_active_annotation_for_comment(
        self,
        annotation_id: UUID,
    ) -> AnnotationRecord:
        """Lock the active annotation that owns a comment operation.

        The shared row lock prevents the annotation from changing between its
        validation and the rest of the comment operation.

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
            raise AnnotationNotFoundError(annotation_id)
        if annotation.status == AnnotationStatus.DELETED:
            raise AnnotationDeletedError(annotation.annotation_id)
        return annotation

    def _get_active_comment_for_change(
        self,
        annotation_id: UUID,
        comment_id: UUID,
    ) -> AnnotationCommentRecord:
        """Get an active comment and prevent concurrent changes to it.

        Args:
            annotation_id: Identifier of the expected parent annotation.
            comment_id: Identifier of the comment being changed.

        Returns:
            The active comment record.

        Raises:
            CommentNotFoundError: If the comment is missing or deleted.
        """
        statement = (
            select(AnnotationCommentRecord)
            .where(
                AnnotationCommentRecord.annotation_id == annotation_id,
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
        """Reject comment text that contains only whitespace."""
        if not body.strip():
            raise InvalidCommentError("comment body must not be blank")
