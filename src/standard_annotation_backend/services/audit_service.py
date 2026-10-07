"""Record application audit events through the current transaction."""

from datetime import datetime
from typing import TypedDict
from uuid import UUID

from standard_annotation_backend.domain.annotations import ChangeSource
from standard_annotation_backend.domain.audit import AuditAction, AuditResult
from standard_annotation_backend.domain.auth import RequestContext
from standard_annotation_backend.persistence.models import AuditEventRecord, JobRecord
from standard_annotation_backend.persistence.repositories import AuditRepository


class _RequestIdentity(TypedDict):
    """Audit values identifying who made a request and with which authorization."""

    actor_id: str
    token_id: str
    token_name: str
    selected_role: str
    selected_scope: str
    selected_group_id: str | None
    result: AuditResult


def _request_identity(context: RequestContext) -> _RequestIdentity:
    """Return the audit values for a successful operation made with a bearer token.

    Args:
        context: Authenticated actor, token identity, and selected authorization.

    Returns:
        Keyword values for `AuditRepository.record` naming the actor, the token
        used, the selected authorization, and a successful result.
    """
    return _RequestIdentity(
        actor_id=context.actor_id,
        token_id=str(context.token_id),
        token_name=context.token_name,
        selected_role=context.role.value,
        selected_scope=context.scope.value,
        selected_group_id=context.group_id,
        result=AuditResult.SUCCESS,
    )


class AuditService:
    """Create audit records for application workflows.

    Args:
        repository: Audit repository sharing the workflow's transaction.
    """

    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository

    def record_change_set_transition(
        self,
        *,
        action: AuditAction,
        context: RequestContext,
        change_set_id: UUID,
        operation: str,
        annotation_id: UUID | None = None,
        annotation_version: int | None = None,
    ) -> AuditEventRecord:
        """Link a successful proposal or review to its resulting annotation version.

        The repository shares the workflow transaction so audit and state changes
        commit together. The immutable request context identifies the user and
        the bearer authorization used for this operation.

        Args:
            action: Change-set operation that completed.
            context: Authenticated user, token identity, and selected authorization.
            change_set_id: Change set affected by the operation.
            operation: Kind of annotation change proposed by the change set.
            annotation_id: Annotation affected or observed, when applicable.
            annotation_version: Annotation version produced or observed, when
                applicable.

        Returns:
            The audit event added to the current transaction.
        """
        return self._repository.record(
            action=action,
            **_request_identity(context),
            change_set_id=change_set_id,
            annotation_id=annotation_id,
            annotation_version=annotation_version,
            details={"change_source": ChangeSource.CHANGE_SET, "operation": operation},
        )

    def record_annotation_mutation(
        self,
        *,
        action: AuditAction,
        context: RequestContext,
        annotation_id: UUID,
        annotation_version: int,
    ) -> AuditEventRecord:
        """Record a successful direct annotation mutation.

        Args:
            action: Direct annotation operation that completed.
            context: Authenticated actor, token identity, and selected authorization.
            annotation_id: Annotation changed by the operation.
            annotation_version: Version produced by the operation.

        Returns:
            The audit event added to the current transaction.
        """
        return self._repository.record(
            action=action,
            **_request_identity(context),
            annotation_id=annotation_id,
            annotation_version=annotation_version,
            details={"change_source": ChangeSource.API},
        )

    def record_annotation_comment_mutation(
        self,
        *,
        action: AuditAction,
        context: RequestContext,
        annotation_id: UUID,
        annotation_version: int,
        comment_id: UUID,
    ) -> AuditEventRecord:
        """Record a successful annotation comment mutation.

        Args:
            action: Comment operation that completed.
            context: Authenticated actor and selected authorization.
            annotation_id: Annotation that owns the comment.
            annotation_version: Annotation version pinned by the comment.
            comment_id: Comment created, edited, or deleted.

        Returns:
            The audit event added to the current transaction.
        """
        return self._repository.record(
            action=action,
            **_request_identity(context),
            annotation_id=annotation_id,
            annotation_version=annotation_version,
            comment_id=comment_id,
            details={"change_source": ChangeSource.API},
        )

    def record_ontology_annotation_update(
        self,
        *,
        actor_id: str,
        job_id: UUID,
        annotation_id: UUID,
        annotation_version: int,
    ) -> AuditEventRecord:
        """Record an annotation update performed during ontology activation."""
        return self._repository.record(
            action=AuditAction.ANNOTATION_UPDATED,
            actor_id=actor_id,
            result=AuditResult.SUCCESS,
            annotation_id=annotation_id,
            annotation_version=annotation_version,
            job_id=job_id,
            details={"change_source": ChangeSource.ONTOLOGY_REFRESH},
        )

    def record_ontology_refreshed(
        self,
        *,
        actor_id: str,
        job_id: UUID,
        details: dict[str, object],
    ) -> AuditEventRecord:
        """Record a summary of a completed ontology activation."""
        return self._record_job_summary(
            AuditAction.ONTOLOGY_REFRESHED, actor_id, job_id, details
        )

    def record_entity_refreshed(
        self, *, actor_id: str, job_id: UUID, details: dict[str, object]
    ) -> AuditEventRecord:
        """Record a summary of a catalog publication in the publishing transaction."""
        return self._record_job_summary(
            AuditAction.ENTITY_REFRESHED, actor_id, job_id, details
        )

    def record_entity_retired(
        self, *, actor_id: str, job_id: UUID, details: dict[str, object]
    ) -> AuditEventRecord:
        """Record a summary of a catalog retirement in the retiring transaction."""
        return self._record_job_summary(
            AuditAction.ENTITY_RETIRED, actor_id, job_id, details
        )

    def record_authorization_refreshed(
        self, *, actor_id: str, job_id: UUID, details: dict[str, object]
    ) -> AuditEventRecord:
        """Record a summary of applied authorization changes in the same transaction.

        Args:
            actor_id: Person or process that requested the refresh.
            job_id: Job that requested the refresh.
            details: Refresh result and counts of users, groups, and assignments.

        Returns:
            The audit event added to the current transaction.
        """
        return self._record_job_summary(
            AuditAction.AUTHORIZATION_REFRESHED, actor_id, job_id, details
        )

    def record_token_created(
        self,
        *,
        user_id: UUID,
        token_id: UUID,
        assignment_id: UUID,
        expires_at: datetime,
    ) -> AuditEventRecord:
        """Record that a user created a bearer token.

        The new token is the target of the event, so it is named in the details
        rather than as the token used to make the request.

        Args:
            user_id: User who created and owns the token.
            token_id: Token that was created.
            assignment_id: Authorization assignment the token acts with.
            expires_at: When the token stops being accepted.

        Returns:
            The audit event added to the current transaction.
        """
        return self._repository.record(
            action=AuditAction.TOKEN_CREATED,
            actor_id=str(user_id),
            result=AuditResult.SUCCESS,
            details={
                "token_id": str(token_id),
                "assignment_id": str(assignment_id),
                "expires_at": expires_at.isoformat(),
            },
        )

    def record_token_revoked(
        self, *, user_id: UUID, token_id: UUID
    ) -> AuditEventRecord:
        """Record that a user revoked one of their bearer tokens.

        Args:
            user_id: User who owns and revoked the token.
            token_id: Token that was revoked.

        Returns:
            The audit event added to the current transaction.
        """
        return self._repository.record(
            action=AuditAction.TOKEN_REVOKED,
            actor_id=str(user_id),
            result=AuditResult.SUCCESS,
            details={"token_id": str(token_id)},
        )

    def _record_job_summary(
        self,
        action: AuditAction,
        actor_id: str,
        job_id: UUID,
        details: dict[str, object],
    ) -> AuditEventRecord:
        """Record a successful job outcome with its summary details."""
        return self._repository.record(
            action=action,
            actor_id=actor_id,
            result=AuditResult.SUCCESS,
            job_id=job_id,
            details=details,
        )

    def record_job_lifecycle(
        self,
        *,
        action: AuditAction,
        record: JobRecord,
        details: dict[str, object] | None = None,
    ) -> AuditEventRecord:
        """Record a job status change. The caller commits the transaction."""
        return self._repository.record(
            action=action,
            actor_id=record.requested_by,
            result=AuditResult.SUCCESS,
            job_id=record.job_id,
            details={
                **(details or {}),
                "job_type": record.job_type,
                "status": record.status,
            },
        )

    def record_annotation_import_published(
        self,
        *,
        is_cutover: bool,
        actor_id: str,
        job_id: UUID,
        details: dict[str, object],
    ) -> AuditEventRecord:
        """Record a summary of a GPAD publication in the publishing transaction."""
        return self._repository.record(
            action=(
                AuditAction.ANNOTATION_CUTOVER_PUBLISHED
                if is_cutover
                else AuditAction.ANNOTATION_REFRESH_PUBLISHED
            ),
            actor_id=actor_id,
            result=AuditResult.SUCCESS,
            job_id=job_id,
            details=details,
        )
