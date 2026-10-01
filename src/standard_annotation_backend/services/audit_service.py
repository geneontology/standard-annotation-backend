"""Record application audit events through the current transaction."""

from uuid import UUID

from standard_annotation_backend.domain.audit import AuditAction, AuditResult
from standard_annotation_backend.domain.auth import RequestContext
from standard_annotation_backend.persistence.models import AuditEventRecord, JobRecord
from standard_annotation_backend.persistence.repositories import AuditRepository


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
            actor_id=context.actor_id,
            token_id=str(context.token_id),
            token_name=context.token_name,
            selected_role=context.role.value,
            selected_scope=context.scope.value,
            selected_group_id=context.group_id,
            result=AuditResult.SUCCESS,
            change_set_id=change_set_id,
            annotation_id=annotation_id,
            annotation_version=annotation_version,
            details={"change_source": "change_set", "operation": operation},
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
            actor_id=context.actor_id,
            token_id=str(context.token_id),
            token_name=context.token_name,
            selected_role=context.role.value,
            selected_scope=context.scope.value,
            selected_group_id=context.group_id,
            result=AuditResult.SUCCESS,
            annotation_id=annotation_id,
            annotation_version=annotation_version,
            details={"change_source": "api"},
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
            actor_id=context.actor_id,
            token_id=str(context.token_id),
            token_name=context.token_name,
            selected_role=context.role.value,
            selected_scope=context.scope.value,
            selected_group_id=context.group_id,
            result=AuditResult.SUCCESS,
            annotation_id=annotation_id,
            annotation_version=annotation_version,
            comment_id=comment_id,
            details={"change_source": "api"},
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
            details={"change_source": "ontology_load"},
        )

    def record_ontology_loaded(
        self,
        *,
        actor_id: str,
        job_id: UUID,
        details: dict[str, object],
    ) -> AuditEventRecord:
        """Record a summary of a completed ontology activation."""
        return self._repository.record(
            action=AuditAction.ONTOLOGY_LOADED,
            actor_id=actor_id,
            result=AuditResult.SUCCESS,
            job_id=job_id,
            details=details,
        )

    def record_entity_catalog_published(
        self, *, actor_id: str, job_id: UUID, details: dict[str, object]
    ) -> AuditEventRecord:
        """Record a summary of a catalog publication in the publishing transaction."""
        return self._repository.record(
            action=AuditAction.ENTITY_CATALOG_PUBLISHED,
            actor_id=actor_id,
            result=AuditResult.SUCCESS,
            job_id=job_id,
            details=details,
        )

    def record_entity_catalog_retired(
        self, *, actor_id: str, job_id: UUID, details: dict[str, object]
    ) -> AuditEventRecord:
        """Record a summary of a catalog retirement in the retiring transaction."""
        return self._repository.record(
            action=AuditAction.ENTITY_CATALOG_RETIRED,
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
