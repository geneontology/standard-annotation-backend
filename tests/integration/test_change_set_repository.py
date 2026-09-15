"""Verify proposal storage and single terminal review transitions."""

from datetime import UTC
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence import models, repositories
from standard_annotation_backend.persistence.locks import (
    GLOBAL_ANNOTATION_WRITE_LOCK_KEY,
)
from standard_annotation_backend.persistence.models import AnnotationOrigin
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory


def _create_target(factory: UnitOfWorkFactory, annotation: Annotation) -> UUID:
    with factory() as unit_of_work:
        record = unit_of_work.annotations.create(
            annotation=annotation,
            actor_id="creator",
            change_source="api",
            owning_group_id="target-group",
            record_origin=AnnotationOrigin.DIRECT,
        )
        unit_of_work.commit()
        return record.annotation_id


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_proposals_store_operation_shape_and_server_metadata(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    operation: str,
) -> None:
    """Proposals persist their payload, target ownership, and server identity/time."""
    annotation_id = (
        None
        if operation == "create"
        else _create_target(unit_of_work_factory, validated_annotation)
    )
    payload = (
        validated_annotation.model_dump(mode="json") if operation == "create" else None
    )
    patch: list[dict[str, object]] | None = (
        [{"op": "replace", "path": "/assigned_by", "value": "TEST"}]
        if operation == "update"
        else None
    )
    with unit_of_work_factory() as unit_of_work:
        record = unit_of_work.change_sets.create(
            operation=operation,
            proposed_by="proposer",
            reason="Review the evidence",
            owning_group_id="create-group" if operation == "create" else None,
            annotation_id=annotation_id,
            base_version=None if operation == "create" else 1,
            annotation_payload=payload,
            patch=patch,
        )
        assert isinstance(record.change_set_id, UUID)
        assert record.proposed_at.tzinfo is UTC
        change_set_id = record.change_set_id
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        stored = unit_of_work.change_sets.get(change_set_id)
        assert stored is not None
        assert stored.operation == operation
        assert stored.state == "proposed"
        assert stored.owning_group_id == (
            "create-group" if operation == "create" else "target-group"
        )
        assert stored.annotation_id == annotation_id
        assert stored.base_version == (None if operation == "create" else 1)
        assert stored.annotation_payload == payload
        assert stored.patch == patch
        assert stored.reason == "Review the evidence"
        assert stored.proposed_by == "proposer"
        assert stored.reviewed_by is None
        assert stored.reviewed_at is None


def _propose_create(factory: UnitOfWorkFactory) -> UUID:
    with factory() as unit_of_work:
        record = unit_of_work.change_sets.create(
            operation="create",
            proposed_by="proposer",
            reason="New evidence",
            owning_group_id="group",
            annotation_payload={"assigned_by": "TEST"},
        )
        unit_of_work.commit()
        return record.change_set_id


def test_preview_updates_metadata_without_transitioning_state(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Preview results survive commit while the proposal remains reviewable."""
    change_set_id = _propose_create(unit_of_work_factory)
    with unit_of_work_factory() as unit_of_work:
        record = unit_of_work.change_sets.record_preview(
            change_set_id, preview={"valid": True}
        )
        assert record.previewed_at is not None
        assert record.previewed_at.tzinfo is UTC
        unit_of_work.commit()
    with unit_of_work_factory() as unit_of_work:
        stored = unit_of_work.change_sets.get(change_set_id)
        assert stored is not None
        assert stored.preview == {"valid": True}
        assert stored.state == "proposed"
        assert stored.reviewed_at is None
        assert stored.reviewed_by is None


@pytest.mark.parametrize(
    "transition,state",
    [("accept", "accepted"), ("reject", "rejected"), ("mark_stale", "stale")],
)
def test_terminal_review_metadata_is_persisted(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    transition: str,
    state: str,
) -> None:
    """Every terminal review records the reviewer, time, and explanation."""
    annotation_id = _create_target(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        proposal = unit_of_work.change_sets.create(
            operation="delete",
            proposed_by="proposer",
            reason="Withdraw evidence",
            annotation_id=annotation_id,
            base_version=1,
        )
        change_set_id = proposal.change_set_id
        unit_of_work.commit()
    with unit_of_work_factory() as unit_of_work:
        record = getattr(unit_of_work.change_sets, transition)(
            change_set_id,
            reviewed_by="reviewer",
            review_reason="Reviewed evidence",
            **({"result_annotation_version": 2} if transition == "accept" else {}),
        )
        assert record.reviewed_at is not None
        assert record.reviewed_at.tzinfo is UTC
        unit_of_work.commit()
    with unit_of_work_factory() as unit_of_work:
        stored = unit_of_work.change_sets.get(change_set_id)
        assert stored is not None
        assert stored.state == state
        assert stored.reviewed_by == "reviewer"
        assert stored.review_reason == "Reviewed evidence"
        assert stored.proposed_by == "proposer"
        assert stored.result_annotation_version == (
            2 if transition == "accept" else None
        )


@pytest.mark.parametrize("first", ["accept", "reject", "mark_stale"])
@pytest.mark.parametrize("second", ["accept", "reject", "mark_stale"])
def test_terminal_transition_cannot_be_repeated_or_changed(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    first: str,
    second: str,
) -> None:
    """A completed review cannot be changed by any later review method."""
    annotation_id = _create_target(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        proposal = unit_of_work.change_sets.create(
            operation="delete",
            proposed_by="proposer",
            reason="Withdraw evidence",
            annotation_id=annotation_id,
            base_version=1,
        )
        change_set_id = proposal.change_set_id
        getattr(unit_of_work.change_sets, first)(
            change_set_id,
            reviewed_by="first-reviewer",
            review_reason="First decision",
            **({"result_annotation_version": 2} if first == "accept" else {}),
        )
        unit_of_work.commit()
    with unit_of_work_factory() as unit_of_work:
        with pytest.raises(repositories.InvalidChangeSetStateError):
            getattr(unit_of_work.change_sets, second)(
                change_set_id,
                reviewed_by="second-reviewer",
                review_reason="Second decision",
                **({"result_annotation_version": 2} if second == "accept" else {}),
            )
        unit_of_work.commit()
    with unit_of_work_factory() as unit_of_work:
        stored = unit_of_work.change_sets.get(change_set_id)
        assert stored is not None
        assert stored.reviewed_by == "first-reviewer"


def test_missing_lookup_and_writes(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Missing proposal reads return None and modifications report not found."""
    with unit_of_work_factory() as unit_of_work:
        missing_id = uuid4()
        assert unit_of_work.change_sets.get(missing_id) is None
        for method in (
            "lock_for_review",
            "accept",
            "reject",
            "mark_stale",
            "record_preview",
        ):
            kwargs: dict[str, object] = (
                {"reviewed_by": "reviewer"}
                if method in {"accept", "reject", "mark_stale"}
                else {}
            )
            if method == "record_preview":
                kwargs = {"preview": {}}
            if method == "accept":
                kwargs["result_annotation_version"] = 1
            if method == "reject":
                kwargs["review_reason"] = "No target"
            with pytest.raises(repositories.ChangeSetNotFoundError):
                getattr(unit_of_work.change_sets, method)(missing_id, **kwargs)


def test_repository_changes_roll_back_without_unit_of_work_commit(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Proposal creation, preview, and terminal review remain caller-owned writes."""
    with unit_of_work_factory() as unit_of_work:
        proposal = unit_of_work.change_sets.create(
            operation="create",
            proposed_by="proposer",
            reason="New evidence",
            owning_group_id="group",
            annotation_payload={},
        )
        discarded_id = proposal.change_set_id
    with unit_of_work_factory() as unit_of_work:
        assert unit_of_work.change_sets.get(discarded_id) is None

    retained_id = _propose_create(unit_of_work_factory)
    with unit_of_work_factory() as unit_of_work:
        unit_of_work.change_sets.record_preview(retained_id, preview={"valid": False})
        unit_of_work.change_sets.reject(
            retained_id, reviewed_by="reviewer", review_reason="Declined"
        )
    with unit_of_work_factory() as unit_of_work:
        stored = unit_of_work.change_sets.get(retained_id)
        assert stored is not None
        assert stored.state == "proposed"
        assert stored.preview is None
        assert stored.reviewed_by is None


def test_proposal_holds_shared_global_write_lock_until_transaction_ends(
    session_factory: sessionmaker[Session],
) -> None:
    """Proposal storage allows shared writers and excludes bootstrap publication."""
    with session_factory() as holder, session_factory() as contender:
        repositories.ChangeSetRepository(holder).create(
            operation="create",
            proposed_by="proposer",
            reason="New evidence",
            owning_group_id="group",
            annotation_payload={},
        )
        assert (
            contender.scalar(
                text("SELECT pg_try_advisory_xact_lock_shared(:key)"),
                {"key": GLOBAL_ANNOTATION_WRITE_LOCK_KEY},
            )
            is True
        )
        contender.rollback()
        assert (
            contender.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": GLOBAL_ANNOTATION_WRITE_LOCK_KEY},
            )
            is False
        )
        contender.rollback()
        holder.rollback()
        assert (
            contender.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": GLOBAL_ANNOTATION_WRITE_LOCK_KEY},
            )
            is True
        )


def test_review_lock_blocks_an_independent_reviewer_and_refreshes_cached_state(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Review locks exclude another transaction and refresh stale session records."""
    change_set_id = _propose_create(unit_of_work_factory)
    with session_factory() as holder, session_factory() as contender:
        holder_repo = repositories.ChangeSetRepository(holder)
        contender_repo = repositories.ChangeSetRepository(contender)
        cached = contender_repo.get(change_set_id)
        assert cached is not None and cached.state == "proposed"
        holder_repo.lock_for_review(change_set_id)
        with pytest.raises(OperationalError):
            contender.execute(
                select(models.ChangeSetRecord)
                .where(models.ChangeSetRecord.change_set_id == change_set_id)
                .with_for_update(nowait=True)
            )
        contender.rollback()
        cached = contender_repo.get(change_set_id)
        assert cached is not None and cached.state == "proposed"
        holder_repo.reject(
            change_set_id, reviewed_by="first-reviewer", review_reason="Declined"
        )
        holder.commit()
        refreshed = contender_repo.lock_for_review(change_set_id)
        assert refreshed.state == "rejected"
        with pytest.raises(repositories.InvalidChangeSetStateError):
            contender_repo.accept(
                change_set_id,
                reviewed_by="second-reviewer",
                result_annotation_version=1,
            )


def test_accepted_create_links_result_and_preserves_proposal(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """Accepting a create stores the assigned annotation identifier and version."""
    change_set_id = _propose_create(unit_of_work_factory)
    annotation_id = _create_target(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        record = unit_of_work.change_sets.accept(
            change_set_id,
            reviewed_by="reviewer",
            annotation_id=annotation_id,
            result_annotation_version=1,
        )
        unit_of_work.commit()
        assert record.annotation_id == annotation_id
        assert record.result_annotation_version == 1
        assert record.base_version is None
        assert record.annotation_payload == {"assigned_by": "TEST"}


@pytest.mark.parametrize("reason", [None, "", " \t\n"])
def test_rejection_requires_nonblank_reason(
    unit_of_work_factory: UnitOfWorkFactory,
    reason: str | None,
) -> None:
    """Rejections without an explanation leave the proposal reviewable."""
    change_set_id = _propose_create(unit_of_work_factory)
    with unit_of_work_factory() as unit_of_work:
        with pytest.raises(ValueError):
            unit_of_work.change_sets.reject(
                change_set_id, reviewed_by="reviewer", review_reason=reason
            )
        unit_of_work.commit()
    with unit_of_work_factory() as unit_of_work:
        record = unit_of_work.change_sets.get(change_set_id)
        assert record is not None
        assert record.state == "proposed"
