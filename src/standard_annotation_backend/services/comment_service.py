"""Coordinate annotation comment operations and authorization."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.auth import (
    PermissionAction,
    RequestContext,
    authorize_role,
)
from standard_annotation_backend.persistence.models import AnnotationCommentRecord
from standard_annotation_backend.persistence.repositories import (
    AuditRepository,
    CommentNotFoundError,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.pagination import ResultPage
from standard_annotation_backend.services.resource_authorization import (
    authorize_annotation,
    load_authorized_annotation,
)


@dataclass(frozen=True, slots=True)
class AnnotationComment:
    """Describe one comment attached to an annotation version.

    Attributes:
        comment_id: Unique identifier for the comment.
        annotation_id: Identifier of the annotation being discussed.
        annotation_version: Annotation version current when the comment was created.
        body: Comment text.
        created_by: Identifier of the comment author.
        created_at: Time when the comment was created.
        updated_at: Time when the comment was last changed.
    """

    comment_id: UUID
    annotation_id: UUID
    annotation_version: int
    body: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class CommentService:
    """Apply annotation access rules and coordinate comment persistence.

    Args:
        unit_of_work_factory: Callable that opens the repositories and transaction
            used by each comment operation.
    """

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def create(
        self,
        annotation_id: UUID,
        *,
        body: str,
        context: RequestContext,
    ) -> AnnotationComment:
        """Create a comment against the annotation's current version.

        Args:
            annotation_id: Identifier of the annotation being discussed.
            body: Nonblank comment text.
            context: Authenticated actor and selected authorization.

        Returns:
            The committed comment.

        Raises:
            AnnotationNotFoundError: If the annotation is absent or inaccessible.
            AnnotationDeletedError: If the annotation is deleted concurrently.
        """
        authorize_role(context, PermissionAction.ANNOTATION_READ)
        with self._unit_of_work_factory() as unit_of_work:
            load_authorized_annotation(
                unit_of_work.annotations,
                context,
                PermissionAction.ANNOTATION_READ,
                annotation_id,
            )
            comment = unit_of_work.comments.create(
                annotation_id,
                body=body,
                created_by=context.actor_id,
            )
            result = _annotation_comment(comment)
            _record_comment_audit(
                unit_of_work.audit,
                action=AuditAction.ANNOTATION_COMMENT_CREATED,
                context=context,
                comment=result,
            )
            unit_of_work.commit()
        return result

    def list(
        self,
        annotation_id: UUID,
        *,
        context: RequestContext,
        limit: int,
        offset: int,
    ) -> ResultPage[AnnotationComment]:
        """List visible comments after authorizing access to the annotation.

        Args:
            annotation_id: Identifier of the annotation being discussed.
            context: Authenticated actor and selected authorization.
            limit: Maximum number of comments to return.
            offset: Number of visible comments to skip.

        Returns:
            Requested page and total visible comment count.

        Raises:
            AnnotationNotFoundError: If the annotation is absent or inaccessible.
            AnnotationDeletedError: If the annotation is deleted concurrently.
        """
        authorize_role(context, PermissionAction.ANNOTATION_READ)
        with self._unit_of_work_factory() as unit_of_work:
            load_authorized_annotation(
                unit_of_work.annotations,
                context,
                PermissionAction.ANNOTATION_READ,
                annotation_id,
            )
            page = unit_of_work.comments.list(
                annotation_id,
                limit=limit,
                offset=offset,
            )
            return ResultPage.from_page(
                page, _annotation_comment, limit=limit, offset=offset
            )

    def edit(
        self,
        annotation_id: UUID,
        comment_id: UUID,
        *,
        body: str,
        context: RequestContext,
    ) -> AnnotationComment:
        """Edit a comment as its author or an annotation administrator.

        Args:
            annotation_id: Identifier of the parent annotation.
            comment_id: Identifier of the comment to edit.
            body: Replacement nonblank comment text.
            context: Authenticated actor and selected authorization.

        Returns:
            The committed updated comment.

        Raises:
            AnnotationNotFoundError: If the annotation is absent or inaccessible.
            AnnotationDeletedError: If the annotation is deleted concurrently.
            CommentNotFoundError: If the comment is absent below the annotation.
            PermissionDeniedError: If the caller is neither the author nor an
                annotation administrator.
        """
        with self._unit_of_work_factory() as unit_of_work:
            comment = _authorized_comment_for_mutation(
                unit_of_work,
                annotation_id=annotation_id,
                comment_id=comment_id,
                context=context,
            )
            updated = unit_of_work.comments.edit(
                annotation_id,
                comment.comment_id,
                body=body,
            )
            result = _annotation_comment(updated)
            _record_comment_audit(
                unit_of_work.audit,
                action=AuditAction.ANNOTATION_COMMENT_UPDATED,
                context=context,
                comment=result,
            )
            unit_of_work.commit()
        return result

    def delete(
        self,
        annotation_id: UUID,
        comment_id: UUID,
        *,
        context: RequestContext,
    ) -> None:
        """Soft-delete a comment as its author or an annotation administrator.

        Args:
            annotation_id: Identifier of the parent annotation.
            comment_id: Identifier of the comment to delete.
            context: Authenticated actor and selected authorization.

        Raises:
            AnnotationNotFoundError: If the annotation is absent or inaccessible.
            AnnotationDeletedError: If the annotation is deleted concurrently.
            CommentNotFoundError: If the comment is absent below the annotation.
            PermissionDeniedError: If the caller is neither the author nor an
                annotation administrator.
        """
        with self._unit_of_work_factory() as unit_of_work:
            comment = _authorized_comment_for_mutation(
                unit_of_work,
                annotation_id=annotation_id,
                comment_id=comment_id,
                context=context,
            )
            deleted = unit_of_work.comments.soft_delete(
                annotation_id,
                comment.comment_id,
            )
            result = _annotation_comment(deleted)
            _record_comment_audit(
                unit_of_work.audit,
                action=AuditAction.ANNOTATION_COMMENT_DELETED,
                context=context,
                comment=result,
            )
            unit_of_work.commit()


def _annotation_comment(record: AnnotationCommentRecord) -> AnnotationComment:
    """Detach a persisted comment from its transaction."""
    return AnnotationComment(
        comment_id=record.comment_id,
        annotation_id=record.annotation_id,
        annotation_version=record.annotation_version,
        body=record.body,
        created_by=record.created_by,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _authorized_comment_for_mutation(
    unit_of_work: SqlAlchemyUnitOfWork,
    *,
    annotation_id: UUID,
    comment_id: UUID,
    context: RequestContext,
) -> AnnotationCommentRecord:
    """Return a visible comment after author or administrator authorization.

    Args:
        unit_of_work: Transaction containing annotation and comment repositories.
        annotation_id: Identifier of the parent annotation.
        comment_id: Identifier of the comment being changed.
        context: Authenticated identity and selected authorization.

    Returns:
        The visible active comment to change.

    Raises:
        AnnotationNotFoundError: If the parent is absent or inaccessible.
        CommentNotFoundError: If the comment is absent below the annotation.
        PermissionDeniedError: If the caller is neither the author nor an annotation
            administrator.
    """
    annotation = load_authorized_annotation(
        unit_of_work.annotations,
        context,
        PermissionAction.ANNOTATION_READ,
        annotation_id,
    )
    comment = unit_of_work.comments.get_for_annotation(annotation_id, comment_id)
    if comment is None:
        raise CommentNotFoundError(comment_id)
    if comment.created_by == context.actor_id:
        return comment
    authorize_role(context, PermissionAction.ANNOTATION_COMMENT_ADMIN)
    authorize_annotation(
        unit_of_work.annotations,
        context,
        PermissionAction.ANNOTATION_COMMENT_ADMIN,
        annotation,
    )
    return comment


def _record_comment_audit(
    repository: AuditRepository,
    *,
    action: AuditAction,
    context: RequestContext,
    comment: AnnotationComment,
) -> None:
    """Add a successful comment mutation to the current transaction.

    Args:
        repository: Audit repository sharing the comment transaction.
        action: Comment operation that completed.
        context: Authenticated actor and selected authorization.
        comment: Comment affected by the operation.
    """
    AuditService(repository).record_annotation_comment_mutation(
        action=action,
        context=context,
        annotation_id=comment.annotation_id,
        annotation_version=comment.annotation_version,
        comment_id=comment.comment_id,
    )
