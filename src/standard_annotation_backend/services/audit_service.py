"""Record application audit events through the current transaction."""

from enum import StrEnum
from uuid import UUID

from standard_annotation_backend.persistence.models import AuditEventRecord
from standard_annotation_backend.persistence.repositories import AuditRepository


class AuditAction(StrEnum):
    """Stable names for operations recorded in the audit trail."""

    ANNOTATION_CREATED = "annotation.created"
    ANNOTATION_UPDATED = "annotation.updated"
    ANNOTATION_DELETED = "annotation.deleted"
    ANNOTATION_COMMENT_CREATED = "annotation_comment.created"
    ANNOTATION_COMMENT_UPDATED = "annotation_comment.updated"
    ANNOTATION_COMMENT_DELETED = "annotation_comment.deleted"
    TOKEN_CREATED = "token.created"
    TOKEN_REVOKED = "token.revoked"
    AUTHORIZATION_SYNCHRONIZED = "authorization.synchronized"
    CHANGE_SET_PROPOSED = "change_set.proposed"
    CHANGE_SET_ACCEPTED = "change_set.accepted"
    CHANGE_SET_REJECTED = "change_set.rejected"
    CHANGE_SET_MARKED_STALE = "change_set.marked_stale"
    IMPORT_COMPLETED = "import.completed"
    EXPORT_COMPLETED = "export.completed"
    ONTOLOGY_LOADED = "ontology.loaded"
    ADMINISTRATIVE_CHANGE = "administrative.change"


class AuditResult(StrEnum):
    """Outcomes stored on audit events."""

    SUCCESS = "success"
    FAILURE = "failure"


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
        actor_id: str,
        change_set_id: UUID,
        operation: str,
        annotation_id: UUID | None = None,
        annotation_version: int | None = None,
    ) -> AuditEventRecord:
        """Link a successful proposal or review to its resulting annotation version.

        The repository shares the workflow transaction so audit and state changes
        commit together. Token context remains null until authentication is added.
        """
        return self._repository.record(
            action=action.value,
            actor_id=actor_id,
            result=AuditResult.SUCCESS.value,
            change_set_id=change_set_id,
            annotation_id=annotation_id,
            annotation_version=annotation_version,
            details={"change_source": "change_set", "operation": operation},
        )

    def record_annotation_mutation(
        self,
        *,
        action: AuditAction,
        actor_id: str,
        annotation_id: UUID,
        annotation_version: int,
    ) -> AuditEventRecord:
        """Record a successful direct annotation mutation.

        Args:
            action: Direct annotation operation that completed.
            actor_id: Identifier for the person or process responsible.
            annotation_id: Annotation changed by the operation.
            annotation_version: Version produced by the operation.

        Returns:
            The audit event added to the current transaction.
        """
        return self._repository.record(
            action=action.value,
            actor_id=actor_id,
            result=AuditResult.SUCCESS.value,
            annotation_id=annotation_id,
            annotation_version=annotation_version,
            details={"change_source": "api"},
        )
