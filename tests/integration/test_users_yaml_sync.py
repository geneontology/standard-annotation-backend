"""Verify atomic refresh of users, grants, provenance, and audit events."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID

import pytest
from refresh_helpers import (
    apply_users_yaml,
    ignore_progress,
    start_job,
    users_document,
)
from source_provenance import github_provenance
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    RefreshOutcome,
    SourceProvenance,
    TerminalRefreshError,
)
from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    AuthorizationAssignmentRecord,
    AuthorizationRefreshRecord,
    JobRecord,
    SabGroupRecord,
    SabUserRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services import authorization_refresh_service
from standard_annotation_backend.services.authorization_refresh_service import (
    AuthorizationRefreshService,
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


def _snapshot(session_factory: sessionmaker[Session]) -> tuple[object, ...]:
    """Return authorization state and every audit event except job lifecycle ones."""
    with session_factory() as session:
        state = tuple(
            tuple(session.execute(select(record).order_by(*record.primary_key)).all())
            for record in (
                SabUserRecord.__table__,
                SabGroupRecord.__table__,
                AuthorizationAssignmentRecord.__table__,
                AuthorizationRefreshRecord.__table__,
            )
        )
        audits = tuple(
            session.execute(
                select(AuditEventRecord.__table__)
                .where(AuditEventRecord.action.not_like("job.%"))
                .order_by(AuditEventRecord.audit_event_id)
            ).all()
        )
        return (*state, audits)


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
    apply_users_yaml(unit_of_work_factory, INITIAL, github_provenance("a" * 40))
    before = _snapshot(session_factory)
    with pytest.raises(TerminalRefreshError) as raised:
        apply_users_yaml(unit_of_work_factory, source, github_provenance("b" * 40))
    assert raised.value.failure_code is RefreshFailureCode.INVALID_DOCUMENT
    assert _snapshot(session_factory) == before


def test_successful_replacement_records_counts_and_preserves_unchanged_contexts(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A replacement updates users and grants and commits matching audit provenance."""
    apply_users_yaml(unit_of_work_factory, INITIAL, github_provenance("a" * 40))
    with unit_of_work_factory() as uow:
        alice = uow.auth.get_user_by_github_login("alice")
        assert alice is not None
        original_user_id = alice.user_id
        original_grants = {
            grant.role: grant.assignment_id
            for grant in uow.auth.list_active_assignments(alice.user_id)
        }

    start = datetime.now(UTC)
    outcome = apply_users_yaml(
        unit_of_work_factory, REPLACEMENT, github_provenance("b" * 40)
    )
    result = outcome.result
    refresh_id = UUID(str(result["refresh_id"]))
    refreshed_at = datetime.fromisoformat(str(result["refreshed_at"]))
    assert (
        result["user_count"],
        result["group_count"],
        result["assignment_count"],
    ) == (3, 2, 3)
    assert result["source_locator"] == "github:geneontology/go-site:metadata/users.yaml"
    assert result["source_revision"] == "b" * 40
    assert start <= refreshed_at <= datetime.now(UTC)
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
        provenance = session.get(AuthorizationRefreshRecord, refresh_id)
        assert provenance is not None
        assert provenance.summary == {"users": 3, "groups": 2, "assignments": 3}
        assert provenance.source_revision == "b" * 40
        assert provenance.refreshed_at == refreshed_at
        audits = session.scalars(
            select(AuditEventRecord)
            .where(AuditEventRecord.action == "authorization.refreshed")
            .order_by(AuditEventRecord.created_at)
        ).all()
        assert len(audits) == 2
        latest = audits[-1]
        assert latest.actor_id == "authorization-refresh"
        assert latest.result == "success"
        assert latest.details == {
            "refresh_id": str(refresh_id),
            "source_type": "github",
            "source_locator": "github:geneontology/go-site:metadata/users.yaml",
            "source_revision": "b" * 40,
            "source_checksum": "b" * 64,
            "fetched_at": result["fetched_at"],
            "refreshed_at": refreshed_at.isoformat(),
            "user_count": 3,
            "group_count": 2,
            "assignment_count": 3,
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
    apply_users_yaml(unit_of_work_factory, INITIAL, github_provenance("a" * 40))
    job = start_job(unit_of_work_factory, JobType.AUTHORIZATION_REFRESH, "go-site")
    before = _snapshot(session_factory)

    def reject_audit(*_: object) -> None:
        raise RuntimeError("audit unavailable")

    event.listen(AuditEventRecord, "before_insert", reject_audit)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            AuthorizationRefreshService(unit_of_work_factory).apply(
                job,
                users_document(REPLACEMENT, github_provenance("b" * 40)),
                ignore_progress,
            )
    finally:
        event.remove(AuditEventRecord, "before_insert", reject_audit)
    assert _snapshot(session_factory) == before


def test_removed_and_identically_regranted_context_never_revives_old_token(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A later identical regrant receives a new ID and leaves old bearer tokens unusable."""
    apply_users_yaml(unit_of_work_factory, INITIAL, github_provenance("a" * 40))
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
    apply_users_yaml(unit_of_work_factory, "[]", github_provenance("b" * 40))
    outcome = apply_users_yaml(
        unit_of_work_factory, INITIAL, github_provenance("c" * 40)
    )
    assert outcome.result["assignment_count"] == 3
    with unit_of_work_factory() as uow:
        assert uow.auth.get_active_assignment(user_id, assignment_id) is None
        assert uow.auth.get_active_token("1" * 64, now=now) is None
        assert len(uow.auth.list_active_assignments(user_id)) == 2


def test_successful_sync_commits_exactly_once(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A synchronization makes its state and audit durable in one commit."""
    job = start_job(unit_of_work_factory, JobType.AUTHORIZATION_REFRESH, "go-site")
    document = users_document(INITIAL, github_provenance("a" * 40))
    commits: list[None] = []

    def committed(_: Session) -> None:
        commits.append(None)

    event.listen(Session, "after_commit", committed)
    try:
        AuthorizationRefreshService(unit_of_work_factory).apply(
            job, document, ignore_progress
        )
    finally:
        event.remove(Session, "after_commit", committed)
    assert len(commits) == 1


def test_repeated_revision_is_idempotent_without_state_or_audit_changes(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Repeating a repository and commit returns the stored result unchanged."""
    first = apply_users_yaml(unit_of_work_factory, INITIAL, github_provenance("a" * 40))
    before = _snapshot(session_factory)

    repeated = apply_users_yaml(
        unit_of_work_factory, INITIAL, github_provenance("a" * 40)
    )

    assert first.unchanged is False
    assert repeated.unchanged is True
    assert repeated.result == first.result
    assert (
        repeated.result["user_count"],
        repeated.result["group_count"],
        repeated.result["assignment_count"],
    ) == (2, 2, 3)
    assert _snapshot(session_factory) == before


def test_concurrent_same_revision_is_applied_once(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent applies of one revision reach the repository's locked check.

    The service's lock-free pre-check is disabled so both requests reach the
    repository, whose locked duplicate check applies the document exactly once.
    """
    monkeypatch.setattr(
        authorization_refresh_service, "same_refresh_source", lambda *_: False
    )
    ready = Barrier(2)

    def synchronize() -> RefreshOutcome:
        ready.wait(timeout=3)
        return apply_users_yaml(
            unit_of_work_factory, INITIAL, github_provenance("a" * 40)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _: synchronize(), range(2)))

    assert {outcome.unchanged for outcome in outcomes} == {True, False}
    assert len({outcome.result["refresh_id"] for outcome in outcomes}) == 1
    with session_factory() as session:
        assert len(session.scalars(select(AuthorizationRefreshRecord)).all()) == 1
        audits = session.scalars(
            select(AuditEventRecord).where(
                AuditEventRecord.action == "authorization.refreshed"
            )
        ).all()
        assert len(audits) == 1


def test_worker_sync_audit_retains_actor_and_job_context(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A job's refresh audit records the job's requester and identifier."""
    outcome = apply_users_yaml(
        unit_of_work_factory,
        INITIAL,
        github_provenance("a" * 40),
        requested_by="scheduler",
    )

    assert outcome.unchanged is False
    with session_factory() as session:
        job_id = session.scalar(
            select(JobRecord.job_id).where(
                JobRecord.job_type == JobType.AUTHORIZATION_REFRESH.value,
                JobRecord.requested_by == "scheduler",
            )
        )
        assert job_id is not None
        audit = session.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.action == "authorization.refreshed"
            )
        )
        assert audit is not None
        assert audit.actor_id == "scheduler"
        assert audit.job_id == job_id


def test_changed_login_creates_new_user_and_deactivates_old_assignments(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A changed source login is a new identity and cannot inherit old credentials."""
    apply_users_yaml(unit_of_work_factory, INITIAL, github_provenance("a" * 40))
    with unit_of_work_factory() as uow:
        original = uow.auth.get_user_by_github_login("alice")
        assert original is not None
        user_id = original.user_id
        grants = {
            item.assignment_id for item in uow.auth.list_active_assignments(user_id)
        }
    apply_users_yaml(
        unit_of_work_factory,
        INITIAL.replace("ALICE", "Renamed-Alice"),
        github_provenance("b" * 40),
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


def _provenance(revision: str, checksum: str) -> SourceProvenance:
    return SourceProvenance(
        source_type="github",
        source_locator="github:geneontology/go-site:metadata/users.yaml",
        source_revision=revision,
        source_checksum=checksum,
        fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_returning_to_an_earlier_document_applies_it_again(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Only the most recent refresh counts as unchanged, so a revert reapplies."""
    first = _provenance("a" * 40, "1" * 64)
    second = _provenance("b" * 40, "2" * 64)

    original = apply_users_yaml(unit_of_work_factory, INITIAL, first)
    assert original.unchanged is False
    assert apply_users_yaml(unit_of_work_factory, INITIAL, second).unchanged is False
    reverted = apply_users_yaml(unit_of_work_factory, INITIAL, first)
    repeated = apply_users_yaml(unit_of_work_factory, INITIAL, first)

    assert reverted.unchanged is False
    assert reverted.result["refresh_id"] != original.result["refresh_id"]
    assert repeated.unchanged is True
    assert repeated.result == reverted.result


def test_https_source_without_revision_is_unchanged_by_checksum(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """An HTTPS source has no revision, so its checksum identifies the document."""
    https = SourceProvenance(
        source_type="https",
        source_locator="https://example.org/users.yaml",
        source_revision=None,
        source_checksum="3" * 64,
        fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    changed = SourceProvenance(
        source_type="https",
        source_locator="https://example.org/users.yaml",
        source_revision=None,
        source_checksum="4" * 64,
        fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
    )

    applied = apply_users_yaml(unit_of_work_factory, INITIAL, https)
    repeated = apply_users_yaml(unit_of_work_factory, INITIAL, https)
    different = apply_users_yaml(unit_of_work_factory, INITIAL, changed)

    assert applied.unchanged is False
    assert repeated.unchanged is True
    assert repeated.result == applied.result
    assert different.unchanged is False
    assert different.result["refresh_id"] != applied.result["refresh_id"]


def test_latest_refresh_follows_apply_order_not_transaction_start(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A transaction that starts first but applies last describes current state."""
    first_applied = github_provenance("a" * 40)
    last_applied = github_provenance("b" * 40)
    with unit_of_work_factory() as late:
        # Start this transaction before the other refresh begins.
        late.auth.session.execute(select(1))
        apply_users_yaml(unit_of_work_factory, INITIAL, first_applied)
        late.auth.replace_authorizations(
            users=(),
            provenance=last_applied,
            summary={"users": 0, "groups": 0, "assignments": 0},
        )
        late.commit()

    with unit_of_work_factory() as uow:
        latest = uow.auth.latest_refresh()
        assert latest is not None
        assert latest.source_revision == "b" * 40
        assert latest.source_checksum == last_applied.source_checksum
        assert latest.source_checksum != first_applied.source_checksum
