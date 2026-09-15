"""Validate, preview, and review proposed annotation changes atomically."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
)

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.change_sets import (
    InvalidChangeSetPatchError,
    apply_annotation_patch,
)
from standard_annotation_backend.domain.validation import (
    AnnotationValidationResult,
    ValidationIssue,
    validate_annotation,
)
from standard_annotation_backend.persistence.annotation_data import (
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationVersionRecord,
    ChangeSetOperation,
    ChangeSetRecord,
    ChangeSetState,
)
from standard_annotation_backend.persistence.repositories import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
    AnnotationRepository,
    StaleAnnotationVersionError,
)
from standard_annotation_backend.persistence.repositories import (
    ChangeSetNotFoundError as RepositoryChangeSetNotFoundError,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.annotation_service import RequestContext
from standard_annotation_backend.services.audit_service import AuditAction, AuditService


class ChangeSetPreview(BaseModel):
    """Describe a proposal's candidate and the current obstacles to acceptance.

    A preview is advisory: acceptance repeats validation and repository checks.
    `annotation` is the candidate payload, or the snapshot to be deleted.
    `before_annotation` is the saved base snapshot for an update or deletion.
    Duplicate peer IDs identify only conflicts that the operation would introduce.
    """

    model_config = ConfigDict(frozen=True)

    change_set_id: UUID
    operation: ChangeSetOperation
    annotation_id: UUID | None = None
    base_version: int | None = None
    current_version: int | None = None
    before_annotation: dict[str, object] | None = None
    annotation: dict[str, object] | None = None
    is_deleted: bool = False
    validation_errors: tuple[ValidationIssue, ...] = ()
    duplicate_peer_ids: tuple[UUID, ...] = ()
    is_stale: bool = False
    can_accept: bool


class ChangeSet(BaseModel):
    """Return proposal data and review metadata detached from its transaction."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    change_set_id: UUID
    operation: ChangeSetOperation
    state: ChangeSetState
    owning_group_id: str
    annotation_id: UUID | None
    base_version: int | None
    annotation_payload: dict[str, object] | None
    patch: list[dict[str, object]] | None
    reason: str
    preview: ChangeSetPreview | None
    previewed_at: datetime | None
    proposed_by: str
    proposed_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_reason: str | None
    result_annotation_version: int | None


class AcceptedChangeSet(BaseModel):
    """Return a completed review and the annotation version it produced."""

    model_config = ConfigDict(frozen=True)

    change_set: ChangeSet
    annotation_id: UUID
    version: int
    is_deleted: bool
    annotation: Annotation


class ChangeSetNotFoundError(LookupError):
    """Report a change-set ID that does not exist."""

    def __init__(self, change_set_id: UUID) -> None:
        self.change_set_id = change_set_id
        super().__init__(f"change set {change_set_id} was not found")


class InvalidChangeSetError(ValueError):
    """Report stable validation issues in a proposal or its candidate annotation."""

    def __init__(self, errors: tuple[ValidationIssue, ...]) -> None:
        self.errors = errors
        super().__init__("change set is invalid")


class ChangeSetStateError(RuntimeError):
    """Report a review operation attempted after a proposal became terminal."""

    def __init__(self, change_set_id: UUID, state: str) -> None:
        self.change_set_id = change_set_id
        self.state = state
        super().__init__(f"change set {change_set_id} is already {state}")


class StaleChangeSetError(RuntimeError):
    """Report a stale proposal after its state and audit event have committed."""

    def __init__(
        self,
        change_set_id: UUID,
        annotation_id: UUID,
        expected_version: int,
        current_version: int,
    ) -> None:
        self.change_set_id = change_set_id
        self.annotation_id = annotation_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"change set {change_set_id} targets an older annotation version"
        )


NonblankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _CreateProposal(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)

    payload: dict[str, JsonValue]
    owning_group_id: NonblankText
    reason: NonblankText


class _TargetProposal(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)

    annotation_id: UUID
    base_version: Annotated[int, Field(gt=0)]
    reason: NonblankText


class _UpdateProposal(_TargetProposal):
    patch: Annotated[list[dict[str, JsonValue]], Field(min_length=1)]


class _Rejection(BaseModel):
    model_config = ConfigDict(strict=True)

    review_reason: NonblankText


class _Acceptance(BaseModel):
    model_config = ConfigDict(strict=True)

    review_reason: str | None


class ChangeSetService:
    """Coordinate proposal, annotation, and audit changes in one transaction.

    Args:
        unit_of_work_factory: Factory opening repositories in one transaction.
    """

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def propose_create(
        self,
        *,
        payload: object,
        owning_group_id: str,
        reason: str,
        context: RequestContext,
    ) -> ChangeSet:
        """Store a candidate for creation, deferring annotation validation to preview."""
        proposal = _validate_input(
            _CreateProposal,
            {
                "payload": payload,
                "owning_group_id": owning_group_id,
                "reason": reason,
            },
        )
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.change_sets.create(
                operation=ChangeSetOperation.CREATE,
                annotation_payload=proposal.model_dump(mode="json")["payload"],
                owning_group_id=proposal.owning_group_id,
                proposed_by=context.actor_id,
                reason=proposal.reason,
            )
            _record_audit(
                unit_of_work, record, AuditAction.CHANGE_SET_PROPOSED, context
            )
            result = ChangeSet.model_validate(record)
            unit_of_work.commit()
        return result

    def propose_update(
        self,
        annotation_id: UUID,
        *,
        base_version: int,
        patch: object,
        reason: str,
        context: RequestContext,
    ) -> ChangeSet:
        """Store a usable JSON Patch targeting an existing annotation snapshot.

        Unsupported patches fail before persistence. A structurally invalid
        annotation produced by a valid patch is retained for preview and review.
        """
        proposal = _validate_input(
            _UpdateProposal,
            {
                "annotation_id": annotation_id,
                "base_version": base_version,
                "patch": patch,
                "reason": reason,
            },
        )
        with self._unit_of_work_factory() as unit_of_work:
            base = _base_snapshot(unit_of_work.annotations, annotation_id, base_version)
            _apply_patch(base.annotation_data, proposal.patch)
            record = unit_of_work.change_sets.create(
                operation=ChangeSetOperation.UPDATE,
                annotation_id=annotation_id,
                base_version=base_version,
                patch=proposal.model_dump(mode="json")["patch"],
                reason=proposal.reason,
                proposed_by=context.actor_id,
            )
            _record_audit(
                unit_of_work, record, AuditAction.CHANGE_SET_PROPOSED, context
            )
            result = ChangeSet.model_validate(record)
            unit_of_work.commit()
        return result

    def get(self, change_set_id: UUID) -> ChangeSet:
        """Retrieve proposal and review data, or raise `ChangeSetNotFoundError`."""
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.change_sets.get(change_set_id)
            if record is None:
                raise ChangeSetNotFoundError(change_set_id)
            return ChangeSet.model_validate(record)

    def propose_delete(
        self,
        annotation_id: UUID,
        *,
        base_version: int,
        reason: str,
        context: RequestContext,
    ) -> ChangeSet:
        """Store a soft-deletion proposal targeting an existing active snapshot."""
        proposal = _validate_input(
            _TargetProposal,
            {
                "annotation_id": annotation_id,
                "base_version": base_version,
                "reason": reason,
            },
        )
        with self._unit_of_work_factory() as unit_of_work:
            _base_snapshot(unit_of_work.annotations, annotation_id, base_version)
            record = unit_of_work.change_sets.create(
                operation=ChangeSetOperation.DELETE,
                annotation_id=annotation_id,
                base_version=base_version,
                reason=proposal.reason,
                proposed_by=context.actor_id,
            )
            _record_audit(
                unit_of_work, record, AuditAction.CHANGE_SET_PROPOSED, context
            )
            result = ChangeSet.model_validate(record)
            unit_of_work.commit()
        return result

    def preview(self, change_set_id: UUID) -> ChangeSetPreview:
        """Validate a candidate and save advisory results without changing annotations."""
        with self._unit_of_work_factory() as unit_of_work:
            record = _lock_proposal(unit_of_work, change_set_id)
            preview = _build_preview(unit_of_work.annotations, record)
            unit_of_work.change_sets.record_preview(
                change_set_id, preview=preview.model_dump(mode="json")
            )
            unit_of_work.commit()
        return preview

    def accept(
        self,
        change_set_id: UUID,
        *,
        context: RequestContext,
        review_reason: str | None = None,
    ) -> AcceptedChangeSet:
        """Repeat validation and repository checks, then accept and audit atomically.

        If a target has changed, commit the stale state and its audit event before
        raising `StaleChangeSetError`, after the unit of work has closed. Other
        validation or duplicate failures roll back and leave the proposal reviewable.
        """
        review = _validate_input(_Acceptance, {"review_reason": review_reason})
        stale_error: StaleChangeSetError | None = None
        result: AcceptedChangeSet | None = None
        with self._unit_of_work_factory() as unit_of_work:
            record = _lock_proposal(unit_of_work, change_set_id)
            try:
                result = _accept_proposal(
                    unit_of_work, record, context, review.review_reason
                )
            except (StaleAnnotationVersionError, AnnotationDeletedError):
                assert (
                    record.annotation_id is not None and record.base_version is not None
                )
                current = _current_target(
                    unit_of_work.annotations, record.annotation_id
                )
                stale_error = StaleChangeSetError(
                    change_set_id,
                    record.annotation_id,
                    record.base_version,
                    current.current_version,
                )
                preview = _build_preview(unit_of_work.annotations, record)
                unit_of_work.change_sets.record_preview(
                    change_set_id, preview=preview.model_dump(mode="json")
                )
                stale = unit_of_work.change_sets.mark_stale(
                    change_set_id,
                    reviewed_by=context.actor_id,
                    review_reason=review.review_reason,
                )
                _record_audit(
                    unit_of_work,
                    stale,
                    AuditAction.CHANGE_SET_MARKED_STALE,
                    context,
                    annotation_version=current.current_version,
                )
            unit_of_work.commit()
        if stale_error is not None:
            raise stale_error
        assert result is not None
        return result

    def reject(
        self,
        change_set_id: UUID,
        *,
        review_reason: str,
        context: RequestContext,
    ) -> ChangeSet:
        """Reject a proposed change with a nonblank explanation and an audit event."""
        review = _validate_input(_Rejection, {"review_reason": review_reason})
        with self._unit_of_work_factory() as unit_of_work:
            _lock_proposal(unit_of_work, change_set_id)
            rejected = unit_of_work.change_sets.reject(
                change_set_id,
                reviewed_by=context.actor_id,
                review_reason=review.review_reason,
            )
            _record_audit(
                unit_of_work, rejected, AuditAction.CHANGE_SET_REJECTED, context
            )
            result = ChangeSet.model_validate(rejected)
            unit_of_work.commit()
        return result


def _accept_proposal(
    unit_of_work: SqlAlchemyUnitOfWork,
    record: ChangeSetRecord,
    context: RequestContext,
    review_reason: str | None,
) -> AcceptedChangeSet:
    """Apply the repository's write policy and record acceptance without committing."""
    if record.annotation_id is not None:
        current = _current_target(unit_of_work.annotations, record.annotation_id)
        assert record.base_version is not None
        if current.current_version != record.base_version:
            raise StaleAnnotationVersionError(
                record.annotation_id, record.base_version, current.current_version
            )
    before, validation = _candidate(unit_of_work.annotations, record)
    if validation.annotation is None:
        raise InvalidChangeSetError(validation.errors)
    if record.operation == ChangeSetOperation.CREATE:
        changed = unit_of_work.annotations.create_direct(
            annotation=validation.annotation,
            owning_group_id=record.owning_group_id,
            actor_id=context.actor_id,
            change_source="change_set",
        )
    else:
        assert record.annotation_id is not None and record.base_version is not None
        if record.operation == ChangeSetOperation.UPDATE:
            changed = unit_of_work.annotations.update_direct(
                record.annotation_id,
                validation.annotation,
                expected_version=record.base_version,
                actor_id=context.actor_id,
                change_source="change_set",
            )
        else:
            changed = unit_of_work.annotations.soft_delete_direct(
                record.annotation_id,
                expected_version=record.base_version,
                actor_id=context.actor_id,
                change_source="change_set",
            )
    preview = ChangeSetPreview(
        change_set_id=record.change_set_id,
        operation=ChangeSetOperation(record.operation),
        annotation_id=record.annotation_id,
        base_version=record.base_version,
        current_version=record.base_version,
        before_annotation=before,
        annotation=validation.annotation.model_dump(mode="json"),
        can_accept=True,
        is_deleted=record.operation == ChangeSetOperation.DELETE,
    )
    unit_of_work.change_sets.record_preview(
        record.change_set_id, preview=preview.model_dump(mode="json")
    )
    accepted = unit_of_work.change_sets.accept(
        record.change_set_id,
        reviewed_by=context.actor_id,
        review_reason=review_reason,
        annotation_id=changed.annotation_id,
        result_annotation_version=changed.current_version,
    )
    _record_audit(unit_of_work, accepted, AuditAction.CHANGE_SET_ACCEPTED, context)
    return AcceptedChangeSet(
        change_set=ChangeSet.model_validate(accepted),
        annotation_id=changed.annotation_id,
        version=changed.current_version,
        is_deleted=changed.status == "deleted",
        annotation=Annotation.model_validate(changed.annotation_data),
    )


def _current_target(
    repository: AnnotationRepository, annotation_id: UUID
) -> AnnotationRecord:
    """Read the target, including deletion metadata needed to recognize staleness."""
    current = repository.get(annotation_id, include_deleted=True)
    if current is None:
        raise AnnotationNotFoundError(annotation_id)
    return current


def _base_snapshot(
    repository: AnnotationRepository,
    annotation_id: UUID,
    base_version: int,
) -> AnnotationVersionRecord:
    """Require a saved version that can serve as an update or deletion base."""
    _current_target(repository, annotation_id)
    base = repository.get_version(annotation_id, base_version)
    if base is None:
        raise InvalidChangeSetError(
            (
                ValidationIssue(
                    location=("base_version",),
                    type="base_version_not_found",
                    message="The target annotation has no such saved version.",
                ),
            )
        )
    if base.is_deleted:
        raise InvalidChangeSetError(
            (
                ValidationIssue(
                    location=("base_version",),
                    type="base_version_deleted",
                    message="The saved version represents a deleted annotation.",
                ),
            )
        )
    return base


def _apply_patch(annotation: dict[str, object], patch: object) -> dict[str, object]:
    """Apply the domain patch policy and translate failures into service issues."""
    try:
        return apply_annotation_patch(annotation, patch)
    except InvalidChangeSetPatchError as error:
        raise InvalidChangeSetError(
            (
                ValidationIssue(
                    location=("patch", *error.issue.location),
                    type=error.issue.type,
                    message="The patch cannot be applied to the annotation.",
                ),
            )
        ) from None


def _candidate(
    repository: AnnotationRepository,
    record: ChangeSetRecord,
) -> tuple[dict[str, object] | None, AnnotationValidationResult]:
    """Validate the proposed payload alongside its immutable base snapshot."""
    if record.operation == ChangeSetOperation.CREATE:
        return None, validate_annotation(record.annotation_payload)
    assert record.annotation_id is not None and record.base_version is not None
    base = _base_snapshot(repository, record.annotation_id, record.base_version)
    if record.operation == ChangeSetOperation.DELETE:
        current = _current_target(repository, record.annotation_id)
        return base.annotation_data, validate_annotation(current.annotation_data)
    try:
        payload = _apply_patch(base.annotation_data, record.patch)
    except InvalidChangeSetError as error:
        return base.annotation_data, AnnotationValidationResult(
            annotation=None, errors=error.errors
        )
    return base.annotation_data, validate_annotation(payload)


def _build_preview(
    repository: AnnotationRepository, record: ChangeSetRecord
) -> ChangeSetPreview:
    """Describe validation, new duplicate conflicts, and observed version drift."""
    before, validation = _candidate(repository, record)
    annotation = validation.annotation
    peers: tuple[UUID, ...] = ()
    current_version = None
    if annotation is not None and record.operation != ChangeSetOperation.DELETE:
        peers = _duplicate_peers(
            repository, annotation, exclude_annotation_id=record.annotation_id
        )
        if before is not None:
            before_peers = _duplicate_peers(
                repository,
                Annotation.model_validate(before),
                exclude_annotation_id=record.annotation_id,
            )
            peers = tuple(sorted(set(peers) - set(before_peers)))
    if record.annotation_id is not None:
        current = _current_target(repository, record.annotation_id)
        current_version = current.current_version
    is_stale = (
        record.base_version is not None and current_version != record.base_version
    )
    return ChangeSetPreview(
        change_set_id=record.change_set_id,
        operation=ChangeSetOperation(record.operation),
        annotation_id=record.annotation_id,
        base_version=record.base_version,
        current_version=current_version,
        before_annotation=before,
        annotation=None if annotation is None else annotation.model_dump(mode="json"),
        validation_errors=validation.errors,
        duplicate_peer_ids=peers,
        is_stale=is_stale,
        is_deleted=record.operation == ChangeSetOperation.DELETE,
        can_accept=annotation is not None and not peers and not is_stale,
    )


def _validate_input[T: BaseModel](model: type[T], payload: object) -> T:
    """Validate workflow input and expose stable annotation-style issue details."""
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise InvalidChangeSetError(
            tuple(
                ValidationIssue(
                    location=issue["loc"], message=issue["msg"], type=issue["type"]
                )
                for issue in error.errors(include_url=False, include_context=False)
            )
        ) from None


def _lock_proposal(
    unit_of_work: SqlAlchemyUnitOfWork, change_set_id: UUID
) -> ChangeSetRecord:
    """Lock a reviewable proposal and translate missing or terminal states."""
    try:
        record = unit_of_work.change_sets.lock_for_review(change_set_id)
    except RepositoryChangeSetNotFoundError:
        raise ChangeSetNotFoundError(change_set_id) from None
    if record.state != ChangeSetState.PROPOSED:
        raise ChangeSetStateError(change_set_id, record.state)
    return record


def _duplicate_peers(
    repository: AnnotationRepository,
    annotation: Annotation,
    *,
    exclude_annotation_id: UUID | None = None,
) -> tuple[UUID, ...]:
    """Look up active peers for advisory preview without performing a write."""
    candidate = prepare_annotation_for_persistence(annotation)
    return repository.find_duplicate_peer_ids(
        candidate.duplicate_base_signature,
        candidate.canonical_references,
        exclude_annotation_id=exclude_annotation_id,
    )


def _record_audit(
    unit_of_work: SqlAlchemyUnitOfWork,
    record: ChangeSetRecord,
    action: AuditAction,
    context: RequestContext,
    *,
    annotation_version: int | None = None,
) -> None:
    """Link a transition to its proposal and optional annotation result or observation."""
    AuditService(unit_of_work.audit).record_change_set_transition(
        action=action,
        actor_id=context.actor_id,
        change_set_id=record.change_set_id,
        annotation_id=record.annotation_id,
        annotation_version=annotation_version
        if annotation_version is not None
        else record.result_annotation_version,
        operation=record.operation,
    )
