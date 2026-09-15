"""Persist annotation change sets and their review state."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from standard_annotation_backend.persistence.locks import (
    acquire_global_annotation_write_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    ChangeSetOperation,
    ChangeSetRecord,
    ChangeSetState,
)
from standard_annotation_backend.persistence.repositories.annotations import (
    AnnotationNotFoundError,
)


class ChangeSetNotFoundError(LookupError):
    """Raised when a review or preview targets an unknown change set."""


class InvalidChangeSetStateError(RuntimeError):
    """Raised when a completed review is asked to transition again."""


class ChangeSetRepository:
    """Persist proposals and review metadata in a caller-managed transaction.

    Review methods lock and re-read the proposal before checking its state.
    They flush changes but never commit; annotation writes and audit events can
    therefore be saved in the same transaction by the calling service.

    Args:
        session: Session shared by the application's unit of work.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        operation: str,
        proposed_by: str,
        reason: str,
        owning_group_id: str | None = None,
        annotation_id: UUID | None = None,
        base_version: int | None = None,
        annotation_payload: dict[str, object] | None = None,
        patch: list[dict[str, object]] | None = None,
    ) -> ChangeSetRecord:
        """Store a proposal under the shared annotation-write lock.

        Create proposals supply their owning group explicitly. Update and delete
        proposals inherit the current target's group from the database. The
        caller validates annotation content and patch semantics before storage;
        database constraints enforce the operation's storage shape.

        Args:
            operation: Requested creation, update, or deletion.
            proposed_by: Actor derived from the authenticated request.
            reason: Explanation supplied for the proposal.
            owning_group_id: Explicit ownership for a create proposal.
            annotation_id: Existing target for an update or delete proposal.
            base_version: Version targeted by an update or delete proposal.
            annotation_payload: Candidate annotation for creation.
            patch: JSON Patch document for an update.

        Returns:
            The stored proposal with a database-generated ID and timestamp.

        Raises:
            AnnotationNotFoundError: If a targeted annotation does not exist.
            ValueError: If the operation or required ownership is invalid.
        """
        operation = ChangeSetOperation(operation)
        acquire_global_annotation_write_lock(self.session)
        if operation == ChangeSetOperation.CREATE:
            if owning_group_id is None:
                raise ValueError("create proposals require an owning group")
        else:
            target = self.session.scalar(
                select(AnnotationRecord)
                .where(AnnotationRecord.annotation_id == annotation_id)
                .execution_options(populate_existing=True)
            )
            if target is None:
                raise AnnotationNotFoundError(
                    f"annotation {annotation_id} was not found"
                )
            owning_group_id = target.owning_group_id
        record = ChangeSetRecord(
            operation=operation.value,
            owning_group_id=owning_group_id,
            annotation_id=annotation_id,
            base_version=base_version,
            annotation_payload=annotation_payload,
            patch=patch,
            proposed_by=proposed_by,
            reason=reason,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, change_set_id: UUID) -> ChangeSetRecord | None:
        """Return a proposal by ID, or None if it does not exist."""
        return self.session.get(ChangeSetRecord, change_set_id)

    def lock_for_review(self, change_set_id: UUID) -> ChangeSetRecord:
        """Lock and freshly read a proposal until the transaction ends.

        The shared global lock is acquired before the row lock to coordinate with
        bootstrap cleanup. Terminal rows can be read under this lock; transition
        methods separately require that the row is still proposed.

        Raises:
            ChangeSetNotFoundError: If no proposal has this ID.
        """
        acquire_global_annotation_write_lock(self.session)
        record = self.session.scalar(
            select(ChangeSetRecord)
            .where(ChangeSetRecord.change_set_id == change_set_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise ChangeSetNotFoundError(f"change set {change_set_id} was not found")
        return record

    def record_preview(
        self,
        change_set_id: UUID,
        *,
        preview: dict[str, object],
    ) -> ChangeSetRecord:
        """Save preview metadata without changing proposal or review state."""
        record = self.lock_for_review(change_set_id)
        record.preview = preview
        record.previewed_at = datetime.now(UTC)
        self.session.flush()
        return record

    def accept(
        self,
        change_set_id: UUID,
        *,
        reviewed_by: str,
        result_annotation_version: int,
        annotation_id: UUID | None = None,
        review_reason: str | None = None,
    ) -> ChangeSetRecord:
        """Record acceptance and the annotation version produced by the service.

        A create acceptance supplies its newly assigned annotation ID. Updates
        and deletions retain the original target ID. The caller owns annotation
        mutation and commits it together with this review.
        """
        record = self._proposed_for_transition(change_set_id)
        if result_annotation_version < 1:
            raise ValueError("accepted proposals require a positive result version")
        if record.operation == ChangeSetOperation.CREATE:
            if annotation_id is None:
                raise ValueError("accepted create proposals require an annotation ID")
        elif annotation_id is not None and annotation_id != record.annotation_id:
            raise ValueError("acceptance cannot change the proposed annotation target")
        if annotation_id is not None:
            record.annotation_id = annotation_id
        record.result_annotation_version = result_annotation_version
        return self._record_transition(
            record, ChangeSetState.ACCEPTED, reviewed_by, review_reason
        )

    def reject(
        self,
        change_set_id: UUID,
        *,
        reviewed_by: str,
        review_reason: str | None = None,
    ) -> ChangeSetRecord:
        """Reject a proposal with a required nonblank explanation."""
        record = self._proposed_for_transition(change_set_id)
        if review_reason is None or not review_reason.strip():
            raise ValueError("rejection requires a nonblank review reason")
        return self._record_transition(
            record, ChangeSetState.REJECTED, reviewed_by, review_reason
        )

    def mark_stale(
        self,
        change_set_id: UUID,
        *,
        reviewed_by: str,
        review_reason: str | None = None,
    ) -> ChangeSetRecord:
        """Record that the service found the proposal's target version stale."""
        record = self._proposed_for_transition(change_set_id)
        return self._record_transition(
            record, ChangeSetState.STALE, reviewed_by, review_reason
        )

    def _proposed_for_transition(self, change_set_id: UUID) -> ChangeSetRecord:
        record = self.lock_for_review(change_set_id)
        if record.state != ChangeSetState.PROPOSED:
            raise InvalidChangeSetStateError(
                f"change set {change_set_id} is no longer proposed"
            )
        return record

    def _record_transition(
        self,
        record: ChangeSetRecord,
        state: ChangeSetState,
        reviewed_by: str,
        review_reason: str | None,
    ) -> ChangeSetRecord:
        record.state = state.value
        record.reviewed_by = reviewed_by
        record.reviewed_at = datetime.now(UTC)
        record.review_reason = review_reason
        self.session.flush()
        return record
