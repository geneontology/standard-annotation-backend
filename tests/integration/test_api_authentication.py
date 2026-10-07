"""Exercise production bearer authentication against disposable PostgreSQL."""

import hashlib
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import pytest
from fastapi import APIRouter, Depends, FastAPI, status
from fastapi.testclient import TestClient
from source_provenance import github_provenance
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.api.dependencies import get_authenticated_context
from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.auth import (
    AuthenticationRequiredError,
    AuthorizationRole,
    AuthorizationScope,
    RequestContext,
)
from standard_annotation_backend.main import app
from standard_annotation_backend.persistence.models import ApiTokenRecord
from standard_annotation_backend.persistence.repositories.auth import (
    SyncAuthorization,
    SyncUser,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory

RAW = "sab_token_" + "a" * 43
MISSING_ID = "00000000-0000-0000-0000-000000000001"
PROTECTED = [
    ("GET", "/annotations"),
    ("POST", "/annotations"),
    ("GET", f"/annotations/{MISSING_ID}"),
    ("PATCH", f"/annotations/{MISSING_ID}"),
    ("DELETE", f"/annotations/{MISSING_ID}"),
    ("GET", f"/annotations/{MISSING_ID}/versions"),
    ("GET", f"/annotations/{MISSING_ID}/versions/1"),
    ("GET", f"/annotations/{MISSING_ID}/comments"),
    ("POST", f"/annotations/{MISSING_ID}/comments"),
    ("PATCH", f"/annotations/{MISSING_ID}/comments/{MISSING_ID}"),
    ("DELETE", f"/annotations/{MISSING_ID}/comments/{MISSING_ID}"),
    ("POST", "/change-sets"),
    ("GET", f"/change-sets/{MISSING_ID}"),
    ("POST", f"/change-sets/{MISSING_ID}/preview"),
    ("POST", f"/change-sets/{MISSING_ID}/accept"),
    ("POST", f"/change-sets/{MISSING_ID}/reject"),
]
BODY_ROUTES = [
    ("POST", "/annotations"),
    ("PATCH", f"/annotations/{MISSING_ID}"),
    ("POST", f"/annotations/{MISSING_ID}/comments"),
    ("PATCH", f"/annotations/{MISSING_ID}/comments/{MISSING_ID}"),
    ("POST", "/change-sets"),
    ("POST", f"/change-sets/{MISSING_ID}/accept"),
    ("POST", f"/change-sets/{MISSING_ID}/reject"),
]


@pytest.fixture
def bearer_client(integration_api_client: TestClient) -> Iterator[TestClient]:
    """Exercise the production dependency while retaining the test database factory."""
    override = app.dependency_overrides.pop(get_authenticated_context)
    try:
        yield integration_api_client
    finally:
        app.dependency_overrides[get_authenticated_context] = override


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_authentication_override_runs_once_and_preserves_context_identity(
    method: str,
) -> None:
    """Dependency caching shares one authenticated context with the operation."""
    test_app = FastAPI()
    router = APIRouter(dependencies=[Depends(get_authenticated_context)])
    issued: list[RequestContext] = []
    received: list[RequestContext] = []

    def authenticate() -> RequestContext:
        context = RequestContext(
            actor_id=f"actor-{len(issued) + 1}",
            token_id=UUID(MISSING_ID),
            token_name="Override",
            role=AuthorizationRole.ADMIN,
            scope=AuthorizationScope.GLOBAL,
            group_id=None,
        )
        issued.append(context)
        return context

    @router.get("/annotations/context")
    def read(
        context: Annotated[RequestContext, Depends(get_authenticated_context)],
    ) -> dict[str, str]:
        received.append(context)
        return {"actor_id": context.actor_id}

    @router.post("/annotations/context")
    def write(
        body: dict[str, str],
        context: Annotated[RequestContext, Depends(get_authenticated_context)],
    ) -> dict[str, str]:
        assert body == {"message": "decoded"}
        received.append(context)
        return {"actor_id": context.actor_id}

    test_app.include_router(router)
    test_app.dependency_overrides[get_authenticated_context] = authenticate
    with TestClient(test_app) as client:
        response = client.request(
            method, "/annotations/context", json={"message": "decoded"}
        )
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"actor_id": "actor-1"}
    assert len(issued) == 1
    assert len(received) == 1 and received[0] is issued[0]


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.usefixtures("active_annotation_subjects")
def test_application_routes_resolve_authentication_override_once(
    integration_api_client: TestClient,
    validated_annotation: Annotation,
    method: str,
) -> None:
    """Registered annotation routes use one overridden identity for the entire operation."""
    existing_override = app.dependency_overrides[get_authenticated_context]
    calls: list[RequestContext] = []

    def authenticate() -> RequestContext:
        context = existing_override()
        calls.append(context)
        return context

    app.dependency_overrides[get_authenticated_context] = authenticate
    try:
        response = integration_api_client.request(
            method,
            "/annotations",
            json={
                "owning_group_id": "MGI",
                "annotation": validated_annotation.model_dump(mode="json"),
            },
        )
    finally:
        app.dependency_overrides[get_authenticated_context] = existing_override
    expected = status.HTTP_200_OK if method == "GET" else status.HTTP_201_CREATED
    assert response.status_code == expected
    assert len(calls) == 1


@pytest.fixture
def bearer_token(unit_of_work_factory: UnitOfWorkFactory) -> dict[str, UUID]:
    """Persist a token selected from one active user's group authorization."""
    with unit_of_work_factory() as uow:
        uow.auth.replace_authorizations(
            users=(
                SyncUser(
                    "curator", "Curator", (SyncAuthorization("edit", "group", "MGI"),)
                ),
            ),
            provenance=github_provenance("a" * 40, "test/repo"),
            summary={},
        )
        owner = uow.auth.get_user_by_github_login("curator")
        assert owner is not None
        assignment = uow.auth.list_active_assignments(owner.user_id)[0]
        token = uow.auth.create_token(
            user_id=owner.user_id,
            assignment_id=assignment.assignment_id,
            name="Notebook",
            digest=hashlib.sha256(RAW.encode()).hexdigest(),
            created_at=datetime.now(UTC) - timedelta(days=2),
            expires_at=datetime.now(UTC) + timedelta(days=5),
        )
        result = {
            "user_id": owner.user_id,
            "token_id": token.token_id,
            "assignment_id": assignment.assignment_id,
        }
        uow.commit()
        return result


@pytest.mark.parametrize("method,path", PROTECTED)
def test_every_data_operation_requires_bearer(
    bearer_client: TestClient,
    method: str,
    path: str,
) -> None:
    """Every data operation rejects absent credentials before accessing application data."""
    response = bearer_client.request(method, path, json={})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {
            "code": "authentication_required",
            "message": "Authentication required",
        }
    }


@pytest.mark.parametrize("method,path", BODY_ROUTES)
def test_malformed_json_is_rejected_without_authenticating(
    bearer_client: TestClient,
    bearer_token: dict[str, UUID],
    session_factory: sessionmaker[Session],
    method: str,
    path: str,
) -> None:
    """Native body parsing reports invalid JSON without recording token use."""
    response = bearer_client.request(
        method,
        path,
        content="{",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {RAW}"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"
    assert response.json()["error"]["details"][0]["type"] == "json_invalid"
    assert "www-authenticate" not in response.headers
    with session_factory() as session:
        token = session.get(ApiTokenRecord, bearer_token["token_id"])
        assert token is not None and token.last_used_at is None


def test_body_requests_record_exactly_one_token_use(
    bearer_client: TestClient,
    bearer_token: dict[str, UUID],
    database_engine: Engine,
) -> None:
    """Router and operation dependencies share one committed token use."""
    writes: list[str] = []

    def observe(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.startswith("UPDATE api_token SET last_used_at="):
            writes.append(statement)

    event.listen(database_engine, "after_cursor_execute", observe)
    try:
        response = bearer_client.post(
            "/annotations",
            content="{}",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {RAW}",
            },
        )
    finally:
        event.remove(database_engine, "after_cursor_execute", observe)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert len(writes) == 1


@pytest.mark.parametrize(
    "credential_state",
    [
        "unknown",
        "expired",
        "revoked",
        "inactive_user",
        "removed_assignment",
        "changed_role",
        "changed_scope",
        "changed_group",
        "renamed_group",
    ],
)
def test_invalid_credential_states_have_identical_public_contract(
    bearer_client: TestClient,
    bearer_token: dict[str, UUID],
    session_factory: sessionmaker[Session],
    credential_state: str,
) -> None:
    """A token loses access after expiry, revocation, or any selected-assignment change."""
    mutations = {
        "expired": "UPDATE api_token SET expires_at = now() - interval '1 day'",
        "revoked": "UPDATE api_token SET revoked_at = now()",
        "inactive_user": "UPDATE sab_user SET is_active = false",
        "removed_assignment": "UPDATE authorization_assignment SET is_active = false",
        "changed_role": "UPDATE authorization_assignment SET role = 'admin'",
        "changed_scope": "UPDATE authorization_assignment SET scope = 'self'",
        "changed_group": "UPDATE authorization_assignment SET scope = 'global', group_id = NULL",
        "renamed_group": "UPDATE sab_group SET group_key = 'OTHER'",
    }
    if credential_state in mutations:
        with session_factory.begin() as session:
            session.execute(text(mutations[credential_state]))
    raw = RAW if credential_state != "unknown" else "sab_token_" + "b" * 43
    response = bearer_client.get(
        "/annotations", headers={"Authorization": f"Bearer {raw}"}
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {
            "code": "authentication_required",
            "message": "Authentication required",
        }
    }
    with session_factory() as session:
        token = session.get(ApiTokenRecord, bearer_token["token_id"])
        assert token is not None and token.last_used_at is None


def test_authentication_returns_complete_immutable_context_and_commits_use(
    unit_of_work_factory: UnitOfWorkFactory,
    bearer_token: dict[str, UUID],
    session_factory: sessionmaker[Session],
) -> None:
    """The returned identity carries the selected token context after last use commits."""
    from standard_annotation_backend.services.authentication_service import (
        AuthenticationService,
    )

    before = datetime.now(UTC)
    context = AuthenticationService(unit_of_work_factory).authenticate(RAW)
    after = datetime.now(UTC)
    assert context.actor_id == str(bearer_token["user_id"])
    assert context.token_id == bearer_token["token_id"]
    assert context.token_name == "Notebook"
    assert context.role is AuthorizationRole.EDIT
    assert context.scope is AuthorizationScope.GROUP
    assert context.group_id == "MGI"
    with pytest.raises(FrozenInstanceError):
        context.actor_id = "other"  # ty: ignore[invalid-assignment] -- Verify runtime immutability.
    with session_factory() as session:
        token = session.get(ApiTokenRecord, bearer_token["token_id"])
        assert token is not None and token.last_used_at is not None
        assert before <= token.last_used_at <= after


def test_valid_bearer_reaches_data_operation(
    bearer_client: TestClient,
    bearer_token: dict[str, UUID],
) -> None:
    """A live bearer token authenticates an annotation read."""
    response = bearer_client.get(
        "/annotations", headers={"Authorization": f"Bearer {RAW}"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"] == []


@pytest.mark.parametrize(
    "headers",
    [
        [("Authorization", "Basic abc")],
        [("Authorization", "Bearer")],
        [("Authorization", "Bearer sab_token_short")],
        [("Authorization", f"Bearer {RAW}, Bearer {RAW}")],
        [("Authorization", f"Bearer {RAW}"), ("Authorization", f"Bearer {RAW}")],
        [("Authorization", "Bearer sab_session_" + "a" * 43)],
    ],
)
def test_malformed_headers_and_management_secrets_cannot_authenticate(
    bearer_client: TestClient,
    bearer_token: dict[str, UUID],
    headers: list[tuple[str, str]],
) -> None:
    """Ambiguous headers and credentials for another purpose never authenticate data routes."""
    response = bearer_client.get("/annotations", headers=headers)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "authentication_required"


def test_unknown_token_service_error_has_no_secret(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """An unknown digest raises only the same credential-neutral domain error."""
    from standard_annotation_backend.services.authentication_service import (
        AuthenticationService,
    )

    with pytest.raises(AuthenticationRequiredError) as raised:
        AuthenticationService(unit_of_work_factory).authenticate(RAW)
    assert str(raised.value) == "Authentication required"


@pytest.mark.parametrize("failure_stage", ["flush", "commit"])
def test_last_use_failure_aborts_authentication_without_disclosing_credentials(
    bearer_client: TestClient,
    bearer_token: dict[str, UUID],
    session_factory: sessionmaker[Session],
    failure_stage: str,
) -> None:
    """A failed last-use transaction rolls back and prevents application data access."""
    digest = hashlib.sha256(RAW.encode()).hexdigest()
    with session_factory.begin() as session:
        session.execute(
            text("""
            CREATE FUNCTION task5_reject_token_use() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'Rejected credential %', NEW.digest;
            END $$
        """)
        )
        if failure_stage == "commit":
            statement = "CREATE CONSTRAINT TRIGGER task5_token_use AFTER UPDATE ON api_token DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION task5_reject_token_use()"
        else:
            statement = "CREATE TRIGGER task5_token_use BEFORE UPDATE ON api_token FOR EACH ROW EXECUTE FUNCTION task5_reject_token_use()"
        session.execute(text(statement))
    try:
        response = bearer_client.get(
            "/annotations", headers={"Authorization": f"Bearer {RAW}"}
        )
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json() == {
            "error": {
                "code": "credential_storage_unavailable",
                "message": "Credential storage is unavailable",
            }
        }
        assert RAW not in response.text and digest not in response.text
        with session_factory() as session:
            token = session.get(ApiTokenRecord, bearer_token["token_id"])
            assert token is not None and token.last_used_at is None
    finally:
        with session_factory.begin() as session:
            session.execute(text("DROP TRIGGER task5_token_use ON api_token"))
            session.execute(text("DROP FUNCTION task5_reject_token_use()"))


@pytest.mark.usefixtures("active_annotation_subjects")
def test_authenticated_write_records_user_and_failed_operation_retains_last_use(
    bearer_client: TestClient,
    bearer_token: dict[str, UUID],
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    """Version history uses the authenticated user regardless of the data outcome."""
    headers = {"Authorization": f"Bearer {RAW}"}
    response = bearer_client.post(
        "/annotations",
        headers=headers,
        json={
            "owning_group_id": "MGI",
            "annotation": validated_annotation.model_dump(mode="json"),
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    versions = bearer_client.get(
        f"/annotations/{response.json()['annotation_id']}/versions", headers=headers
    )
    assert versions.status_code == status.HTTP_200_OK
    assert versions.json()["items"][0]["actor_id"] == str(bearer_token["user_id"])
    before = datetime.now(UTC)
    missing = bearer_client.get(f"/annotations/{MISSING_ID}", headers=headers)
    assert missing.status_code == status.HTTP_404_NOT_FOUND
    with session_factory() as session:
        token = session.get(ApiTokenRecord, bearer_token["token_id"])
        assert token is not None and token.last_used_at is not None
        assert token.last_used_at >= before


@pytest.mark.parametrize("scope,group", [("self", "MGI"), ("global", None)])
def test_other_valid_selections_authenticate(
    unit_of_work_factory: UnitOfWorkFactory,
    scope: str,
    group: str | None,
) -> None:
    """Self and global tokens retain their original scope and group selection."""
    from standard_annotation_backend.services.authentication_service import (
        AuthenticationService,
    )

    with unit_of_work_factory() as uow:
        uow.auth.replace_authorizations(
            users=(
                SyncUser(
                    "curator", "Curator", (SyncAuthorization("read", scope, group),)
                ),
            ),
            provenance=github_provenance("b" * 40, "test/repo"),
            summary={},
        )
        owner = uow.auth.get_user_by_github_login("curator")
        assert owner is not None
        grant = uow.auth.list_active_assignments(owner.user_id)[0]
        uow.auth.create_token(
            user_id=owner.user_id,
            assignment_id=grant.assignment_id,
            name="Read client",
            digest=hashlib.sha256(RAW.encode()).hexdigest(),
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        uow.commit()
    context = AuthenticationService(unit_of_work_factory).authenticate(RAW)
    assert context.role is AuthorizationRole.READ
    assert context.scope.value == scope
    assert context.group_id == group
