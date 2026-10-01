"""Verify bearer role and ownership policy through the real database and API."""

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.api.dependencies import get_authenticated_context
from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.main import app
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationVersionRecord,
    AuditEventRecord,
    AuthorizationAssignmentRecord,
    ChangeSetRecord,
)
from standard_annotation_backend.persistence.repositories.auth import (
    SyncAuthorization,
    SyncUser,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory

RAW_TOKEN = "sab_token_" + "z" * 43
ROLE_SCOPES = [
    (role, scope)
    for role in ("read", "edit", "admin")
    for scope in ("self", "group", "global")
]
PERMISSION_ERROR = {
    "error": {"code": "permission_denied", "message": "Permission denied"}
}
ANNOTATION_MISSING = {
    "error": {"code": "annotation_not_found", "message": "Annotation was not found"}
}
CHANGE_SET_MISSING = {
    "error": {"code": "change_set_not_found", "message": "Change set was not found"}
}


@pytest.fixture(autouse=True)
def active_authorization_subjects(seed_active_subjects: Callable[..., None]) -> None:
    """Seed subjects so authorized operations reach the ownership workflow."""
    seed_active_subjects(
        "UniProtKB:P12345",
        *(f"UniProtKB:{name}" for name in ("own", "own2", "other", "foreign")),
        *(f"UniProtKB:proposed-{name}" for name in ("own", "own2", "other", "foreign")),
        "UniProtKB:create-None",
        "UniProtKB:create-MGI",
        "UniProtKB:create-RGD",
    )


@pytest.fixture
def bearer_client(integration_api_client: TestClient) -> Iterator[TestClient]:
    """Authenticate real bearer credentials against the disposable test database."""
    override = app.dependency_overrides.pop(get_authenticated_context)
    try:
        yield integration_api_client
    finally:
        app.dependency_overrides[get_authenticated_context] = override


@dataclass(frozen=True, slots=True)
class Resources:
    actor: str
    token_id: UUID
    assignment_id: UUID
    annotations: dict[str, UUID]
    updates: dict[str, UUID]
    creates: dict[str, UUID]


def _seed(
    factory: UnitOfWorkFactory, annotation: Annotation, role: str, scope: str
) -> Resources:
    """Create credentials and interleaved ownership fixtures with immutable creators."""
    with factory() as uow:
        uow.auth.replace_authorizations(
            users=(
                SyncUser(
                    "curator",
                    "Curator",
                    (
                        SyncAuthorization(
                            role, scope, None if scope == "global" else "MGI"
                        ),
                    ),
                ),
                SyncUser(
                    "reviewer",
                    "Reviewer",
                    (SyncAuthorization("admin", "global", None),),
                ),
            ),
            source_repository="test/repo",
            source_commit_sha="a" * 40,
            summary={},
        )
        user = uow.auth.get_user_by_github_login("curator")
        assert user is not None
        assignment = uow.auth.list_active_assignments(user.user_id)[0]
        token = uow.auth.create_token(
            user_id=user.user_id,
            assignment_id=assignment.assignment_id,
            name="Authorization tests",
            digest=hashlib.sha256(RAW_TOKEN.encode()).hexdigest(),
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        annotations: dict[str, UUID] = {}
        updates: dict[str, UUID] = {}
        creates: dict[str, UUID] = {}
        for name, creator, group in (
            ("foreign", str(user.user_id), "RGD"),
            ("other", "another-actor", "MGI"),
            ("own", str(user.user_id), "MGI"),
            ("own2", str(user.user_id), "MGI"),
        ):
            payload = annotation.model_dump(mode="json") | {
                "db_object_id": f"UniProtKB:{name}"
            }
            record = uow.annotations.create_direct(
                annotation=Annotation.model_validate(payload),
                actor_id=creator,
                owning_group_id=group,
            )
            annotations[name] = record.annotation_id
            proposal = uow.change_sets.create(
                operation="update",
                annotation_id=record.annotation_id,
                base_version=1,
                patch=[{"op": "replace", "path": "/assigned_by", "value": "Reviewed"}],
                reason="Review",
                proposed_by="different-proposer",
            )
            updates[name] = proposal.change_set_id
            create = uow.change_sets.create(
                operation="create",
                owning_group_id=group,
                annotation_payload=payload
                | {"db_object_id": f"UniProtKB:proposed-{name}"},
                reason="Review",
                proposed_by=creator,
            )
            creates[name] = create.change_set_id
            record.created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(
                seconds=len(annotations)
            )
        result = Resources(
            str(user.user_id),
            token.token_id,
            assignment.assignment_id,
            annotations,
            updates,
            creates,
        )
        uow.commit()
        return result


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {RAW_TOKEN}", "If-Match": '"1"'}


def _snapshot(factory: sessionmaker[Session]) -> tuple[object, ...]:
    """Observe all operation state from an independent database connection."""
    with factory() as session:
        return (
            tuple(
                session.execute(
                    select(
                        AnnotationRecord.annotation_id,
                        AnnotationRecord.current_version,
                        AnnotationRecord.status,
                    ).order_by(AnnotationRecord.annotation_id)
                )
            ),
            tuple(
                session.execute(
                    select(
                        AnnotationVersionRecord.annotation_id,
                        AnnotationVersionRecord.version,
                    ).order_by(
                        AnnotationVersionRecord.annotation_id,
                        AnnotationVersionRecord.version,
                    )
                )
            ),
            tuple(
                session.execute(
                    select(
                        ChangeSetRecord.change_set_id,
                        ChangeSetRecord.state,
                        ChangeSetRecord.previewed_at,
                        ChangeSetRecord.reviewed_at,
                    ).order_by(ChangeSetRecord.change_set_id)
                )
            ),
            tuple(
                session.scalars(
                    select(AuditEventRecord.audit_event_id).order_by(
                        AuditEventRecord.audit_event_id
                    )
                )
            ),
        )


@pytest.mark.parametrize("role,scope", ROLE_SCOPES)
def test_reads_and_pagination_follow_creator_and_group_before_counting(
    bearer_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    role: str,
    scope: str,
) -> None:
    """Every role reads only its scope, including version history and stored proposals."""
    resources = _seed(unit_of_work_factory, validated_annotation, role, scope)
    visible = {
        "self": ["own", "own2"],
        "group": ["other", "own", "own2"],
        "global": ["foreign", "other", "own", "own2"],
    }[scope]
    response = bearer_client.get("/annotations?limit=1&offset=1", headers=_headers())
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == len(visible)
    assert [item["annotation_id"] for item in response.json()["items"]] == [
        str(resources.annotations[visible[1]])
    ]
    empty_page = bearer_client.get(
        "/annotations?limit=1&offset=99", headers=_headers()
    ).json()
    assert empty_page["items"] == [] and empty_page["total"] == len(visible)
    filtered = bearer_client.get(
        "/annotations?db_object_id=UniProtKB:foreign", headers=_headers()
    ).json()
    assert filtered["total"] == (1 if scope == "global" else 0)
    for name in ("own", "other", "foreign"):
        permitted = name in visible
        annotation_id = resources.annotations[name]
        for suffix in ("", "/versions", "/versions/1"):
            read = bearer_client.get(
                f"/annotations/{annotation_id}{suffix}", headers=_headers()
            )
            assert read.status_code == (
                status.HTTP_200_OK if permitted else status.HTTP_404_NOT_FOUND
            )
            if not permitted:
                assert read.json() == ANNOTATION_MISSING
        for proposal_id in (resources.updates[name], resources.creates[name]):
            read = bearer_client.get(f"/change-sets/{proposal_id}", headers=_headers())
            assert read.status_code == (
                status.HTTP_200_OK if permitted else status.HTTP_404_NOT_FOUND
            )
            if not permitted:
                assert read.json() == CHANGE_SET_MISSING


@pytest.mark.parametrize("role,scope", ROLE_SCOPES)
@pytest.mark.parametrize(
    "operation",
    [
        "patch",
        "delete",
        "propose_update",
        "propose_delete",
        "preview",
        "accept",
        "reject",
    ],
)
def test_mutations_enforce_roles_and_scope_without_partial_state(
    bearer_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    role: str,
    scope: str,
    operation: str,
) -> None:
    """Denied actions preserve annotation, proposal, version, and audit state."""
    resources = _seed(unit_of_work_factory, validated_annotation, role, scope)
    role_allowed = (
        role == "admin" if operation in {"accept", "reject"} else role != "read"
    )
    for name in ("own", "other", "foreign"):
        scope_allowed = (
            name == "own" or scope == "global" or (name == "other" and scope == "group")
        )
        before = _snapshot(session_factory)
        annotation_id = resources.annotations[name]
        if operation in {"patch", "delete"}:
            response = bearer_client.request(
                operation.upper(),
                f"/annotations/{annotation_id}",
                headers=_headers(),
                json={"assigned_by": "Reviewed"} if operation == "patch" else None,
            )
        elif operation.startswith("propose_"):
            body: dict[str, object] = {
                "operation": operation.removeprefix("propose_"),
                "annotation_id": str(annotation_id),
                "base_version": 1,
                "reason": "Review",
            }
            if operation == "propose_update":
                body["patch"] = [
                    {"op": "replace", "path": "/assigned_by", "value": "Reviewed"}
                ]
            response = bearer_client.post("/change-sets", json=body, headers=_headers())
        else:
            response = bearer_client.post(
                f"/change-sets/{resources.updates[name]}/{operation}",
                headers=_headers(),
                json={"review_reason": "Reviewed"} if operation == "reject" else None,
            )
        if not role_allowed:
            assert response.status_code == status.HTTP_403_FORBIDDEN
            assert response.json() == PERMISSION_ERROR
        elif not scope_allowed:
            assert response.status_code == status.HTTP_404_NOT_FOUND
            assert response.json() == (
                CHANGE_SET_MISSING
                if operation in {"preview", "accept", "reject"}
                else ANNOTATION_MISSING
            )
        else:
            expected = (
                status.HTTP_204_NO_CONTENT
                if operation == "delete"
                else status.HTTP_201_CREATED
                if operation.startswith("propose_")
                else status.HTTP_200_OK
            )
            assert response.status_code == expected, response.text
        if not role_allowed or not scope_allowed:
            assert _snapshot(session_factory) == before


@pytest.mark.parametrize("role,scope", ROLE_SCOPES)
@pytest.mark.parametrize("proposal", [False, True])
def test_creation_derives_group_and_requires_explicit_global_ownership(
    bearer_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    role: str,
    scope: str,
    proposal: bool,
) -> None:
    """Creation respects selected roles and never accepts a conflicting group."""
    _seed(unit_of_work_factory, validated_annotation, role, scope)
    route = "/change-sets" if proposal else "/annotations"
    for group in (None, "MGI", "RGD"):
        payload = validated_annotation.model_dump(mode="json") | {
            "db_object_id": f"UniProtKB:create-{group}"
        }
        body: dict[str, object] = {"annotation": payload}
        if proposal:
            body.update(operation="create", reason="Review")
        if group is not None:
            body["owning_group_id"] = group
        before = _snapshot(session_factory)
        response = bearer_client.post(route, headers=_headers(), json=body)
        permitted = role != "read" and (
            group is not None if scope == "global" else group in {None, "MGI"}
        )
        assert response.status_code == (
            status.HTTP_201_CREATED if permitted else status.HTTP_403_FORBIDDEN
        ), response.text
        if permitted:
            assert response.json()["owning_group_id"] == (
                group if scope == "global" else "MGI"
            )
        else:
            assert response.json() == PERMISSION_ERROR
            assert _snapshot(session_factory) == before


@pytest.mark.parametrize("scope", ["self", "group"])
def test_deleted_history_is_authorized_before_version_disclosure(
    bearer_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    scope: str,
) -> None:
    """Deleted annotations retain their current ownership boundary for all history."""
    resources = _seed(unit_of_work_factory, validated_annotation, "read", scope)
    with unit_of_work_factory() as uow:
        for annotation_id in resources.annotations.values():
            uow.annotations.soft_delete_direct(
                annotation_id, actor_id="deleter", expected_version=1
            )
        uow.commit()
    for name in ("own", "foreign"):
        for suffix in ("versions", "versions/1", "versions/2", "versions/99"):
            response = bearer_client.get(
                f"/annotations/{resources.annotations[name]}/{suffix}",
                headers=_headers(),
            )
            if name == "foreign":
                assert response.status_code == status.HTTP_404_NOT_FOUND
                assert response.json() == ANNOTATION_MISSING
            elif suffix != "versions/99":
                assert response.status_code == status.HTTP_200_OK


def test_removed_authorization_rejects_an_issued_token_before_mutation(
    bearer_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    """Assignment removal invalidates already issued credentials on the next request."""
    resources = _seed(unit_of_work_factory, validated_annotation, "edit", "group")
    with session_factory.begin() as session:
        assignment = session.get(AuthorizationAssignmentRecord, resources.assignment_id)
        assert assignment is not None
        assignment.is_active = False
    before = _snapshot(session_factory)
    response = bearer_client.delete(
        f"/annotations/{resources.annotations['own']}", headers=_headers()
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert _snapshot(session_factory) == before


@pytest.mark.parametrize("scope", ["self", "group"])
@pytest.mark.parametrize("kind", ["update", "create"])
@pytest.mark.parametrize("state", ["proposed", "accepted", "rejected", "stale"])
def test_hidden_change_sets_never_disclose_proposer_or_terminal_state(
    bearer_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    scope: str,
    kind: str,
    state: str,
) -> None:
    """Out-of-scope proposals use the missing-resource contract before every state check."""
    resources = _seed(unit_of_work_factory, validated_annotation, "admin", scope)
    hidden = "other" if scope == "self" else "foreign"
    proposal_id = (resources.creates if kind == "create" else resources.updates)[hidden]
    with unit_of_work_factory() as uow:
        if state == "accepted":
            uow.change_sets.accept(
                proposal_id,
                reviewed_by="reviewer",
                result_annotation_version=1,
                annotation_id=resources.annotations[hidden]
                if kind == "create"
                else None,
            )
        elif state == "rejected":
            uow.change_sets.reject(
                proposal_id, reviewed_by="reviewer", review_reason="Private review"
            )
        elif state == "stale":
            uow.change_sets.mark_stale(proposal_id, reviewed_by="reviewer")
        uow.commit()
    before = _snapshot(session_factory)
    for operation in ("get", "preview", "accept", "reject"):
        responses = []
        for target in (proposal_id, uuid4()):
            if operation == "get":
                response = bearer_client.get(
                    f"/change-sets/{target}", headers=_headers()
                )
            else:
                response = bearer_client.post(
                    f"/change-sets/{target}/{operation}",
                    headers=_headers(),
                    json={"review_reason": "Reviewed"}
                    if operation == "reject"
                    else None,
                )
            assert response.status_code == status.HTTP_404_NOT_FOUND
            responses.append(response.json())
        assert responses == [CHANGE_SET_MISSING, CHANGE_SET_MISSING]
        assert _snapshot(session_factory) == before


@pytest.mark.parametrize("scope", ["self", "group"])
def test_duplicate_conflicts_disclose_all_peer_ids(
    bearer_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    scope: str,
) -> None:
    """Duplicate conflicts identify every peer regardless of authorization scope."""
    resources = _seed(unit_of_work_factory, validated_annotation, "admin", scope)
    hidden_name = "other" if scope == "self" else "foreign"
    peer_id = str(resources.annotations[hidden_name])
    payload = validated_annotation.model_dump(mode="json") | {
        "db_object_id": f"UniProtKB:{hidden_name}"
    }
    before = _snapshot(session_factory)
    create = bearer_client.post(
        "/annotations",
        headers=_headers(),
        json={"owning_group_id": "MGI", "annotation": payload},
    )
    assert create.status_code == status.HTTP_409_CONFLICT
    assert create.json()["error"]["details"]["peer_ids"] == [peer_id]
    assert _snapshot(session_factory) == before
    proposed = bearer_client.post(
        "/change-sets",
        headers=_headers(),
        json={
            "operation": "create",
            "annotation": payload,
            "owning_group_id": "MGI",
            "reason": "Review",
        },
    )
    assert proposed.status_code == status.HTTP_201_CREATED
    proposal_id = proposed.json()["change_set_id"]
    preview = bearer_client.post(
        f"/change-sets/{proposal_id}/preview", headers=_headers()
    )
    assert preview.status_code == status.HTTP_200_OK
    assert preview.json()["duplicate_peer_ids"] == [peer_id]
    assert preview.json()["can_accept"] is False
    before = _snapshot(session_factory)
    accepted = bearer_client.post(
        f"/change-sets/{proposal_id}/accept", headers=_headers()
    )
    assert accepted.status_code == status.HTTP_409_CONFLICT
    assert accepted.json()["error"]["details"]["peer_ids"] == [peer_id]
    assert _snapshot(session_factory) == before


def test_stored_preview_returns_all_duplicate_peer_ids(
    bearer_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """Reading a saved preview returns every recorded duplicate peer ID."""
    resources = _seed(unit_of_work_factory, validated_annotation, "read", "self")
    proposal_id = resources.creates["own"]
    with unit_of_work_factory() as uow:
        uow.change_sets.record_preview(
            proposal_id,
            preview={
                "change_set_id": str(proposal_id),
                "operation": "create",
                "can_accept": False,
                "duplicate_peer_ids": [
                    str(resources.annotations["foreign"]),
                    str(resources.annotations["own"]),
                ],
            },
        )
        uow.commit()
    response = bearer_client.get(f"/change-sets/{proposal_id}", headers=_headers())
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["preview"]["duplicate_peer_ids"] == [
        str(resources.annotations["foreign"]),
        str(resources.annotations["own"]),
    ]
    assert response.json()["preview"]["can_accept"] is False


def test_accepted_creation_keeps_proposer_self_access_and_reviewer_audit(
    bearer_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    """Accepting a trainee's creation retains its creator and attributes review to the admin."""
    resources = _seed(unit_of_work_factory, validated_annotation, "edit", "self")
    review_raw = "sab_token_" + "r" * 43
    with unit_of_work_factory() as uow:
        reviewer = uow.auth.get_user_by_github_login("reviewer")
        assert reviewer is not None
        assignment = uow.auth.list_active_assignments(reviewer.user_id)[0]
        token = uow.auth.create_token(
            user_id=reviewer.user_id,
            assignment_id=assignment.assignment_id,
            name="Reviewer",
            digest=hashlib.sha256(review_raw.encode()).hexdigest(),
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        reviewer_id, token_id = str(reviewer.user_id), str(token.token_id)
        uow.commit()
    accepted = bearer_client.post(
        f"/change-sets/{resources.creates['own']}/accept",
        headers={"Authorization": f"Bearer {review_raw}"},
    )
    assert accepted.status_code == status.HTTP_200_OK
    annotation_id = accepted.json()["annotation_id"]
    own_read = bearer_client.get(f"/annotations/{annotation_id}", headers=_headers())
    assert own_read.status_code == status.HTTP_200_OK
    history = bearer_client.get(
        f"/annotations/{annotation_id}/versions/1", headers=_headers()
    )
    assert history.status_code == status.HTTP_200_OK
    assert history.json()["actor_id"] == resources.actor
    stored = bearer_client.get(
        f"/change-sets/{resources.creates['own']}", headers=_headers()
    )
    assert stored.status_code == status.HTTP_200_OK
    assert stored.json()["reviewed_by"] == reviewer_id
    with session_factory() as session:
        audit = session.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.action == "change_set.accepted"
            )
        )
        assert audit is not None
        assert audit.actor_id == reviewer_id and audit.token_id == token_id
        assert audit.token_name == "Reviewer"
        assert audit.selected_role == "admin" and audit.selected_scope == "global"


def test_all_mutation_audits_persist_the_full_bearer_selection_without_secrets(
    bearer_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    """Successful direct changes and all proposal transitions record safe token metadata."""
    resources = _seed(unit_of_work_factory, validated_annotation, "admin", "group")
    payload = validated_annotation.model_dump(mode="json")
    created = bearer_client.post(
        "/annotations", headers=_headers(), json={"annotation": payload}
    )
    assert created.status_code == status.HTTP_201_CREATED
    proposed = bearer_client.post(
        "/change-sets",
        headers=_headers(),
        json={"operation": "create", "annotation": payload, "reason": "Review"},
    )
    assert proposed.status_code == status.HTTP_201_CREATED
    accepted = bearer_client.post(
        f"/change-sets/{resources.updates['own']}/accept", headers=_headers()
    )
    assert accepted.status_code == status.HTTP_200_OK
    rejected = bearer_client.post(
        f"/change-sets/{resources.updates['other']}/reject",
        headers=_headers(),
        json={"review_reason": "Reviewed"},
    )
    assert rejected.status_code == status.HTTP_200_OK
    patched = bearer_client.patch(
        f"/annotations/{resources.annotations['own2']}",
        headers=_headers(),
        json={"assigned_by": "New source"},
    )
    assert patched.status_code == status.HTTP_200_OK
    stale = bearer_client.post(
        f"/change-sets/{resources.updates['own2']}/accept", headers=_headers()
    )
    assert stale.status_code == status.HTTP_409_CONFLICT
    deleted = bearer_client.delete(
        f"/annotations/{resources.annotations['own2']}",
        headers=_headers() | {"If-Match": '"2"'},
    )
    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    with session_factory() as session:
        events = list(session.scalars(select(AuditEventRecord)))
        assert {event.action for event in events} == {
            "annotation.created",
            "annotation.updated",
            "annotation.deleted",
            "change_set.proposed",
            "change_set.accepted",
            "change_set.rejected",
            "change_set.marked_stale",
        }
        for event in events:
            assert event.actor_id == resources.actor
            assert event.token_id == str(resources.token_id)
            assert event.token_name == "Authorization tests"
            assert event.selected_role == "admin"
            assert event.selected_scope == "group"
            assert event.selected_group_id == "MGI"
            serialized = repr(
                {
                    column.name: getattr(event, column.name)
                    for column in AuditEventRecord.__table__.columns
                }
            )
            assert RAW_TOKEN not in serialized
            assert hashlib.sha256(RAW_TOKEN.encode()).hexdigest() not in serialized
