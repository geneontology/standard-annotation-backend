"""Verify synchronized authorization and user credential persistence."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.audit import AuditAction, AuditResult
from standard_annotation_backend.persistence import models
from standard_annotation_backend.persistence.repositories.auth import (
    AuthRepository,
    SyncAuthorization,
    SyncUser,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory


def _sync(
    unit_of_work_factory: UnitOfWorkFactory, source_commit_sha: str = "a" * 40
) -> None:
    with unit_of_work_factory() as uow:
        uow.auth.replace_authorizations(
            users=(
                SyncUser(
                    "curator", "Curator", (SyncAuthorization("edit", "group", "MGI"),)
                ),
                SyncUser("other", None, (SyncAuthorization("admin", "global", None),)),
            ),
            source_repository="geneontology/go-site",
            source_commit_sha=source_commit_sha,
            summary={"users": 2},
        )
        uow.commit()


def test_sync_retains_unchanged_ids_and_preserves_removed_history(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Removal invalidates grants permanently while unchanged grants retain identity."""
    _sync(unit_of_work_factory)
    with unit_of_work_factory() as uow:
        user = uow.auth.get_user_by_github_login("CURATOR")
        assert user is not None
        user_id = user.user_id
        original = uow.auth.list_active_assignments(user_id)[0].assignment_id
    _sync(unit_of_work_factory)
    with unit_of_work_factory() as uow:
        assert uow.auth.list_active_assignments(user_id)[0].assignment_id == original
        uow.auth.replace_authorizations(
            users=(SyncUser("curator", "Renamed", ()),),
            source_repository="geneontology/go-site",
            source_commit_sha="b" * 40,
            summary={"users": 1},
        )
        assert uow.auth.list_active_assignments(user_id) == ()
        assert uow.auth.get_active_assignment(user_id, original) is None
        assert uow.auth.get_user_by_github_login("other") is None
        uow.commit()
    _sync(unit_of_work_factory, "c" * 40)
    with unit_of_work_factory() as uow:
        assert uow.auth.list_active_assignments(user_id)[0].assignment_id != original
    with session_factory() as session:
        removed = session.get(models.AuthorizationAssignmentRecord, original)
        assert removed is not None
        assert removed.is_active is False
        sync = session.scalars(select(models.AuthorizationSyncRecord)).first()
        assert sync is not None
        assert sync.source_repository == "geneontology/go-site"
        assert sync.source_commit_sha == "a" * 40
        assert sync.summary == {"users": 2}


def test_auth_and_audit_share_transaction_and_rollback(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Exiting without commit rolls back synchronized identities and audit together."""
    with unit_of_work_factory() as uow:
        assert uow.auth.session is uow.audit.session
        uow.auth.replace_authorizations(
            users=(SyncUser("curator", None, ()),),
            source_repository="repo",
            source_commit_sha="c" * 40,
            summary={},
        )
        uow.audit.record(
            action=AuditAction.AUTHORIZATION_SYNCHRONIZED,
            actor_id="sync",
            result=AuditResult.SUCCESS,
        )
    with session_factory() as session:
        for record in (
            models.SabUserRecord,
            models.AuthorizationSyncRecord,
            models.AuditEventRecord,
        ):
            assert session.scalar(select(func.count()).select_from(record)) == 0


def test_token_listing_revocation_and_last_use_are_owner_scoped(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Tokens expose safe metadata and only their owner can revoke them."""
    _sync(unit_of_work_factory)
    now = datetime.now(UTC)
    with unit_of_work_factory() as uow:
        owner = uow.auth.get_user_by_github_login("curator")
        other = uow.auth.get_user_by_github_login("other")
        assert owner is not None
        assert other is not None
        assignment = uow.auth.list_active_assignments(owner.user_id)[0]
        token = uow.auth.create_token(
            user_id=owner.user_id,
            assignment_id=assignment.assignment_id,
            name="Notebook",
            digest="a" * 64,
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        loaded = uow.auth.get_active_token("a" * 64, now=now)
        assert loaded is not None
        assert loaded.owner.github_login == "curator"
        assert loaded.assignment.group is not None
        assert loaded.assignment.group.group_key == "MGI"
        assert (
            uow.auth.get_active_assignment(other.user_id, assignment.assignment_id)
            is None
        )
        uow.auth.record_token_use(token.token_id, used_at=now + timedelta(seconds=1))
        uow.auth.record_token_use(token.token_id, used_at=now)
        entries = uow.auth.list_tokens(owner.user_id)
        assert len(entries) == 1
        assert entries[0].name == "Notebook"
        assert entries[0].last_used_at == now + timedelta(seconds=1)
        assert uow.auth.list_tokens(other.user_id) == ()
        assert (
            uow.auth.revoke_token(other.user_id, token.token_id, revoked_at=now)
            is False
        )
        assert uow.auth.revoke_token(owner.user_id, uuid4(), revoked_at=now) is False
        assert (
            uow.auth.revoke_token(owner.user_id, token.token_id, revoked_at=now) is True
        )
        assert uow.auth.get_active_token("a" * 64, now=now) is None
        assert (
            uow.auth.revoke_token(
                owner.user_id, token.token_id, revoked_at=now + timedelta(seconds=1)
            )
            is True
        )
        assert uow.auth.list_tokens(owner.user_id)[0].revoked_at == now
        uow.commit()


def test_digest_lookup_rejects_expiry_inactive_assignments_and_missing_credentials(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Expired or removed authorization contexts cannot authenticate credentials."""
    _sync(unit_of_work_factory)
    now = datetime.now(UTC)
    with unit_of_work_factory() as uow:
        owner = uow.auth.get_user_by_github_login("curator")
        assert owner is not None
        assignment = uow.auth.list_active_assignments(owner.user_id)[0]
        uow.auth.create_token(
            user_id=owner.user_id,
            assignment_id=assignment.assignment_id,
            name="Client",
            digest="b" * 64,
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
        management = uow.auth.create_management_session(
            user_id=owner.user_id,
            digest="c" * 64,
            created_at=now,
            expires_at=now + timedelta(minutes=15),
        )
        loaded = uow.auth.get_active_management_session("c" * 64, now=now)
        assert loaded is not None
        assert loaded.owner.user_id == owner.user_id
        assert uow.auth.get_active_token("d" * 64, now=now) is None
        assert uow.auth.get_active_management_session("d" * 64, now=now) is None
        assert uow.auth.get_active_token("b" * 64, now=now + timedelta(days=1)) is None
        assert (
            uow.auth.get_active_management_session(
                "c" * 64, now=now + timedelta(minutes=15)
            )
            is None
        )
        assignment.is_active = False
        uow.auth.session.flush()
        assert uow.auth.get_active_token("b" * 64, now=now) is None
        management.revoked_at = now
        uow.auth.session.flush()
        assert uow.auth.get_active_management_session("c" * 64, now=now) is None
        management.revoked_at = None
        assignment.is_active = True
        owner.is_active = False
        uow.auth.session.flush()
        assert uow.auth.get_active_token("b" * 64, now=now) is None
        assert uow.auth.get_active_management_session("c" * 64, now=now) is None


def test_failed_sync_preserves_previous_state_and_provenance(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A failing replacement rolls back new users, grants, and its provenance."""
    _sync(unit_of_work_factory)
    with pytest.raises(IntegrityError), unit_of_work_factory() as uow:
        uow.auth.replace_authorizations(
            users=(
                SyncUser("new", None, (SyncAuthorization("invalid", "global", None),)),
            ),
            source_repository="repo",
            source_commit_sha="b" * 40,
            summary={},
        )
    with unit_of_work_factory() as uow:
        assert uow.auth.get_user_by_github_login("new") is None
        curator = uow.auth.get_user_by_github_login("curator")
        assert curator is not None
        assert len(uow.auth.list_active_assignments(curator.user_id)) == 1
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(models.AuthorizationSyncRecord)
            )
            == 1
        )


def test_duplicate_grants_in_one_sync_share_assignment_identity(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Repeated identical entries produce a single assignment per distinct context."""
    grant = SyncAuthorization("edit", "group", "MGI")
    global_grant = SyncAuthorization("read", "global", None)
    with unit_of_work_factory() as uow:
        uow.auth.replace_authorizations(
            users=(
                SyncUser("curator", None, (grant, grant, global_grant, global_grant)),
            ),
            source_repository="repo",
            source_commit_sha="a" * 40,
            summary={},
        )
        owner = uow.auth.get_user_by_github_login("curator")
        assert owner is not None
        assignments = uow.auth.list_active_assignments(owner.user_id)
        assert {(item.role, item.scope) for item in assignments} == {
            ("edit", "group"),
            ("read", "global"),
        }
        assert len(assignments) == 2
        uow.commit()


def test_removed_and_regranted_context_does_not_revive_issued_token(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Regranting identical authority cannot make an old token authenticate again."""
    _sync(unit_of_work_factory)
    now = datetime.now(UTC)
    with unit_of_work_factory() as uow:
        owner = uow.auth.get_user_by_github_login("curator")
        assert owner is not None
        assignment = uow.auth.list_active_assignments(owner.user_id)[0]
        uow.auth.create_token(
            user_id=owner.user_id,
            assignment_id=assignment.assignment_id,
            name="Client",
            digest="a" * 64,
            created_at=now,
            expires_at=now + timedelta(days=1),
        )
        uow.commit()
    with unit_of_work_factory() as uow:
        uow.auth.replace_authorizations(
            users=(), source_repository="repo", source_commit_sha="b" * 40, summary={}
        )
        uow.commit()
    _sync(unit_of_work_factory, "c" * 40)
    with unit_of_work_factory() as uow:
        assert uow.auth.get_active_token("a" * 64, now=now) is None
        entries = uow.auth.list_tokens(owner.user_id)
        assert len(entries) == 1
        assert entries[0].assignment_is_active is False


def test_concurrent_syncs_replace_the_complete_committed_state(
    session_factory: sessionmaker[Session],
) -> None:
    """Independent sync transactions serialize before reading and replacing grants."""
    started = Event()
    finished = Event()
    with (
        session_factory() as first,
        session_factory() as second,
        session_factory() as observer,
    ):
        for session in (first, second, observer):
            session.execute(text("SET LOCAL statement_timeout = '5s'"))
        first_pid = first.scalar(text("SELECT pg_backend_pid()"))
        second_pid = second.scalar(text("SELECT pg_backend_pid()"))
        observer_pid = observer.scalar(text("SELECT pg_backend_pid()"))
        assert len({first_pid, second_pid, observer_pid}) == 3
        AuthRepository(first).replace_authorizations(
            users=(
                SyncUser("first", None, (SyncAuthorization("edit", "group", "MGI"),)),
            ),
            source_repository="repo",
            source_commit_sha="a" * 40,
            summary={},
        )

        def synchronize_second() -> None:
            started.set()
            try:
                AuthRepository(second).replace_authorizations(
                    users=(
                        SyncUser(
                            "second", None, (SyncAuthorization("read", "global", None),)
                        ),
                    ),
                    source_repository="repo",
                    source_commit_sha="b" * 40,
                    summary={},
                )
                second.commit()
            finally:
                second.rollback()
                finished.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(synchronize_second)
            try:
                assert started.wait(3)
                deadline = monotonic() + 3
                blocked = False
                while monotonic() < deadline:
                    blockers = observer.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid}
                    )
                    if first_pid in blockers:
                        blocked = True
                        break
                    if finished.wait(0.01):
                        break
                assert blocked, (
                    "second synchronization must wait for the complete first transaction"
                )
                first.commit()
            finally:
                first.rollback()
            future.result(timeout=6)
    with session_factory() as session:
        repository = AuthRepository(session)
        assert repository.get_user_by_github_login("first") is None
        owner = repository.get_user_by_github_login("second")
        assert owner is not None
        assert len(repository.list_active_assignments(owner.user_id)) == 1
        assert (
            session.scalar(
                select(func.count()).select_from(models.AuthorizationSyncRecord)
            )
            == 2
        )
