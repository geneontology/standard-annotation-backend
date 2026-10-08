"""Verify the change-set repository's caller-owned transactions and write locks."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.persistence import repositories
from standard_annotation_backend.persistence.locks import (
    GLOBAL_ANNOTATION_WRITE_LOCK_KEY,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory


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
