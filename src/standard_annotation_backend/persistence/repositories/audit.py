"""Write audit events in caller-managed transactions."""

from uuid import UUID

from sqlalchemy.orm import Session

from standard_annotation_backend.persistence.models import AuditEventRecord


class AuditRepository:
    """Write audit events in a caller-managed transaction.

    The repository flushes events so database errors are raised before the
    caller commits the surrounding operation.

    Args:
        session: Session shared with the operation being audited.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        action: str,
        actor_id: str,
        result: str,
        token_id: str | None = None,
        token_name: str | None = None,
        selected_role: str | None = None,
        selected_scope: str | None = None,
        selected_group_id: str | None = None,
        annotation_id: UUID | None = None,
        annotation_version: int | None = None,
        comment_id: UUID | None = None,
        job_id: UUID | None = None,
        change_set_id: UUID | None = None,
        details: dict[str, object] | None = None,
    ) -> AuditEventRecord:
        """Record who performed an operation and which resources it affected.

        Args:
            action: Stable name of the operation.
            actor_id: Identifier for the person or process responsible.
            result: Outcome of the operation.
            token_id: Token used for the operation, when available.
            token_name: Human-readable token name, when available.
            selected_role: Role selected by the token, when available.
            selected_scope: Scope selected by the token, when available.
            selected_group_id: Group selected by the token, when available.
            annotation_id: Annotation affected by the operation, when applicable.
            annotation_version: Annotation version produced or observed.
            comment_id: Comment affected by the operation, when applicable.
            job_id: Job associated with the operation, when applicable.
            change_set_id: Change set associated with the operation, when applicable.
            details: Additional structured context for the operation.

        Returns:
            The newly stored audit event.
        """
        event = AuditEventRecord(
            action=action,
            actor_id=actor_id,
            result=result,
            token_id=token_id,
            token_name=token_name,
            selected_role=selected_role,
            selected_scope=selected_scope,
            selected_group_id=selected_group_id,
            annotation_id=annotation_id,
            annotation_version=annotation_version,
            comment_id=comment_id,
            job_id=job_id,
            change_set_id=change_set_id,
            details={} if details is None else details,
        )
        self.session.add(event)
        self.session.flush([event])
        return event
