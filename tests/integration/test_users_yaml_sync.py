"""Verify atomic synchronization of users, grants, provenance, and audit events."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.auth.users_yaml import InvalidUsersDocumentError
from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    AuthorizationAssignmentRecord,
    AuthorizationSyncRecord,
    SabGroupRecord,
    SabUserRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.authorization_sync_service import (
    AuthorizationSyncResult,
    AuthorizationSyncService,
)

INITIAL = """
- nickname: Original Name
  accounts: {github: ALICE}
  authorizations:
    sab:
      - {role: edit, scope: group, group: zfin}
      - {role: read, scope: group, group: mgi}
- accounts: {github: bob}
  authorizations:
    sab: [{role: read, scope: group, group: zfin}]
"""
REPLACEMENT = """
- nickname: Updated Name
  accounts: {github: alice}
  groups: [unrelated-legacy-group]
  authorizations:
    sab:
      - {role: edit, scope: group, group: zfin}
      - {role: edit, scope: self, group: flybase}
      - {role: edit, scope: self, group: flybase}
- accounts: {github: carol}
  authorizations:
    sab: [{role: admin, scope: global}]
- accounts: {github: reader}
- nickname: Legacy person without GitHub
"""


def _service(factory: UnitOfWorkFactory) -> AuthorizationSyncService:
    return AuthorizationSyncService(factory)


def _snapshot(session_factory: sessionmaker[Session]) -> tuple[object, ...]:
    with session_factory() as session:
        return tuple(
            tuple(session.execute(select(record).order_by(*record.primary_key)).all())
            for record in (
                SabUserRecord.__table__,
                SabGroupRecord.__table__,
                AuthorizationAssignmentRecord.__table__,
                AuthorizationSyncRecord.__table__,
                AuditEventRecord.__table__,
            )
        )


@pytest.mark.parametrize(
    "source",
    [
        "[",
        INITIAL + "- authorizations: {sab: [{role: owner, scope: global}]}\n",
        "!!set {}",
        "- accounts: {github: alice}\n  authorizations: {sab: !!set {}}",
    ],
)
def test_invalid_sync_preserves_all_last_valid_state(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    source: str,
) -> None:
    """A malformed document cannot replace any grants or their provenance."""
    service = _service(unit_of_work_factory)
    service.synchronize(INITIAL, "geneontology/go-site", "a" * 40)
    before = _snapshot(session_factory)
    with pytest.raises(InvalidUsersDocumentError):
        service.synchronize(source, "geneontology/go-site", "b" * 40)
    assert _snapshot(session_factory) == before


def test_successful_replacement_records_counts_and_preserves_unchanged_contexts(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A replacement updates users and grants and commits matching audit provenance."""
    service = _service(unit_of_work_factory)
    service.synchronize(INITIAL, "geneontology/go-site", "a" * 40)
    with unit_of_work_factory() as uow:
        alice = uow.auth.get_user_by_github_login("alice")
        assert alice is not None
        original_user_id = alice.user_id
        original_grants = {
            grant.role: grant.assignment_id
            for grant in uow.auth.list_active_assignments(alice.user_id)
        }

    start = datetime.now(UTC)
    result = service.synchronize(REPLACEMENT, "geneontology/go-site", "B" * 40)
    assert (result.user_count, result.group_count, result.assignment_count) == (3, 2, 3)
    assert result.source_repository == "geneontology/go-site"
    assert result.source_commit_sha == "b" * 40
    assert start <= result.synchronized_at <= datetime.now(UTC)
    with unit_of_work_factory() as uow:
        alice = uow.auth.get_user_by_github_login("ALICE")
        assert alice is not None
        assert alice.user_id == original_user_id
        assert alice.display_name == "Updated Name"
        assert {
            (grant.role, grant.scope, grant.group.group_key if grant.group else None)
            for grant in uow.auth.list_active_assignments(alice.user_id)
        } == {("edit", "group", "zfin"), ("edit", "self", "flybase")}
        assert (
            uow.auth.get_active_assignment(alice.user_id, original_grants["edit"])
            is not None
        )
        assert (
            uow.auth.get_active_assignment(alice.user_id, original_grants["read"])
            is None
        )
        assert uow.auth.get_user_by_github_login("bob") is None
        assert uow.auth.get_user_by_github_login("carol") is not None
        reader = uow.auth.get_user_by_github_login("reader")
        assert reader is not None
        assert uow.auth.list_active_assignments(reader.user_id) == ()

    with session_factory() as session:
        provenance = session.get(AuthorizationSyncRecord, result.sync_id)
        assert provenance is not None
        assert provenance.summary == {"users": 3, "groups": 2, "assignments": 3}
        assert provenance.source_commit_sha == "b" * 40
        assert provenance.synchronized_at == result.synchronized_at
        audits = session.scalars(
            select(AuditEventRecord).order_by(AuditEventRecord.created_at)
        ).all()
        assert len(audits) == 2
        latest = audits[-1]
        assert latest.action == "authorization.synchronized"
        assert latest.actor_id == "authorization-sync"
        assert latest.result == "success"
        assert latest.details == {
            "sync_id": str(result.sync_id),
            "source_repository": "geneontology/go-site",
            "source_commit_sha": "b" * 40,
            "synchronized_at": result.synchronized_at.isoformat(),
            "users": 3,
            "groups": 2,
            "assignments": 3,
        }
        assert (
            "unrelated-legacy-group"
            not in session.scalars(select(SabGroupRecord.group_key)).all()
        )


def test_audit_failure_rolls_back_sync_and_its_provenance(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A failed audit insertion restores the complete previous authorization state."""
    service = _service(unit_of_work_factory)
    service.synchronize(INITIAL, "geneontology/go-site", "a" * 40)
    before = _snapshot(session_factory)

    def reject_audit(*_: object) -> None:
        raise RuntimeError("audit unavailable")

    event.listen(AuditEventRecord, "before_insert", reject_audit)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            service.synchronize(REPLACEMENT, "geneontology/go-site", "b" * 40)
    finally:
        event.remove(AuditEventRecord, "before_insert", reject_audit)
    assert _snapshot(session_factory) == before


def test_removed_and_identically_regranted_context_never_revives_old_token(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A later identical regrant receives a new ID and leaves old bearer tokens unusable."""
    service = _service(unit_of_work_factory)
    service.synchronize(INITIAL, "geneontology/go-site", "a" * 40)
    now = datetime.now(UTC)
    with unit_of_work_factory() as uow:
        alice = uow.auth.get_user_by_github_login("alice")
        assert alice is not None
        user_id = alice.user_id
        assignment_id = uow.auth.list_active_assignments(user_id)[0].assignment_id
        uow.auth.create_token(
            user_id=user_id,
            assignment_id=assignment_id,
            name="old-context",
            digest="1" * 64,
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
        uow.commit()
    service.synchronize("[]", "geneontology/go-site", "b" * 40)
    result = service.synchronize(INITIAL, "geneontology/go-site", "c" * 40)
    assert result.assignment_count == 3
    with unit_of_work_factory() as uow:
        assert uow.auth.get_active_assignment(user_id, assignment_id) is None
        assert uow.auth.get_active_token("1" * 64, now=now) is None
        assert len(uow.auth.list_active_assignments(user_id)) == 2


def test_successful_sync_commits_exactly_once(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A synchronization makes its state and audit durable in one commit."""
    commits: list[None] = []

    def committed(_: Session) -> None:
        commits.append(None)

    event.listen(Session, "after_commit", committed)
    try:
        _service(unit_of_work_factory).synchronize(
            INITIAL, "geneontology/go-site", "a" * 40
        )
    finally:
        event.remove(Session, "after_commit", committed)
    assert len(commits) == 1


def test_repeated_revision_is_idempotent_without_state_or_audit_changes(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Repeating a repository and commit returns the stored result unchanged."""
    service = _service(unit_of_work_factory)
    first = service.synchronize(INITIAL, "geneontology/go-site", "a" * 40)
    before = _snapshot(session_factory)

    repeated = service.synchronize(INITIAL, "geneontology/go-site", "a" * 40)

    assert first.applied is True
    assert repeated.applied is False
    assert repeated.sync_id == first.sync_id
    assert (
        repeated.user_count,
        repeated.group_count,
        repeated.assignment_count,
    ) == (2, 2, 3)
    assert _snapshot(session_factory) == before


def test_concurrent_same_revision_is_applied_once(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Concurrent requests for one source revision create one sync and audit."""
    ready = Barrier(2)

    def synchronize() -> AuthorizationSyncResult:
        ready.wait(timeout=3)
        return _service(unit_of_work_factory).synchronize(
            INITIAL, "geneontology/go-site", "a" * 40
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: synchronize(), range(2)))

    assert {result.applied for result in results} == {True, False}
    assert len({result.sync_id for result in results}) == 1
    with session_factory() as session:
        assert len(session.scalars(select(AuthorizationSyncRecord)).all()) == 1
        audits = session.scalars(
            select(AuditEventRecord).where(
                AuditEventRecord.action == "authorization.synchronized"
            )
        ).all()
        assert len(audits) == 1


def test_worker_sync_audit_retains_actor_and_job_context(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A worker-created sync audit records its actor and job identifier."""
    job_id = uuid4()

    result = _service(unit_of_work_factory).synchronize(
        INITIAL,
        "geneontology/go-site",
        "a" * 40,
        actor_id="scheduler",
        job_id=job_id,
    )

    assert result.applied is True
    with session_factory() as session:
        audit = session.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.action == "authorization.synchronized"
            )
        )
        assert audit is not None
        assert audit.actor_id == "scheduler"
        assert audit.job_id == job_id


def test_changed_login_creates_new_user_and_deactivates_old_assignments(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A changed source login is a new identity and cannot inherit old credentials."""
    service = _service(unit_of_work_factory)
    service.synchronize(INITIAL, "geneontology/go-site", "a" * 40)
    with unit_of_work_factory() as uow:
        original = uow.auth.get_user_by_github_login("alice")
        assert original is not None
        user_id = original.user_id
        grants = {
            item.assignment_id for item in uow.auth.list_active_assignments(user_id)
        }
    service.synchronize(
        INITIAL.replace("ALICE", "Renamed-Alice"), "geneontology/go-site", "b" * 40
    )
    with unit_of_work_factory() as uow:
        renamed = uow.auth.get_user_by_github_login("renamed-alice")
        assert renamed is not None and renamed.user_id != user_id
        assert {
            item.assignment_id
            for item in uow.auth.list_active_assignments(renamed.user_id)
        }.isdisjoint(grants)
        assert uow.auth.list_active_assignments(user_id) == ()
        assert uow.auth.get_user_by_github_login("alice") is None
