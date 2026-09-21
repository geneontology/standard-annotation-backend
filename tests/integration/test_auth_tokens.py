"""Exercise token self-service through real services and PostgreSQL persistence."""

import hashlib
import json
import traceback
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.auth import AuthenticationRequiredError
from standard_annotation_backend.main import app
from standard_annotation_backend.persistence.models import (
    ApiTokenRecord,
    AuditEventRecord,
    AuthorizationAssignmentRecord,
    SabGroupRecord,
    TokenManagementSessionRecord,
)
from standard_annotation_backend.persistence.repositories.auth import (
    SyncAuthorization,
    SyncUser,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.authentication_service import (
    AuthenticationService,
)
from standard_annotation_backend.services.authorization_sync_service import (
    AuthorizationSyncService,
)
from standard_annotation_backend.services.token_service import (
    InvalidTokenError,
    TokenService,
)


@pytest.fixture
def user_assignments(unit_of_work_factory: UnitOfWorkFactory) -> dict[str, UUID]:
    """Seed two independent users and a global and group context for the curator."""
    with unit_of_work_factory() as uow:
        uow.auth.replace_authorizations(
            users=(
                SyncUser(
                    "curator",
                    "Curator",
                    (
                        SyncAuthorization("edit", "group", "MGI"),
                        SyncAuthorization("read", "global", None),
                    ),
                ),
                SyncUser(
                    "other",
                    "Other",
                    (SyncAuthorization("admin", "global", None),),
                ),
            ),
            source_repository="test/repo",
            source_commit_sha="a" * 40,
            summary={},
        )
        curator = uow.auth.get_user_by_github_login("curator")
        other = uow.auth.get_user_by_github_login("other")
        assert curator is not None and other is not None
        assignments = uow.auth.list_active_assignments(curator.user_id)
        result = {
            "user_id": curator.user_id,
            "other_user_id": other.user_id,
            "assignment_id": next(
                item.assignment_id for item in assignments if item.group is not None
            ),
            "other_assignment_id": uow.auth.list_active_assignments(other.user_id)[
                0
            ].assignment_id,
        }
        uow.commit()
        return result


def _login(client: TestClient) -> None:
    first = client.get("/auth/github/login", follow_redirects=False)
    assert first.status_code == status.HTTP_302_FOUND
    state = parse_qs(urlsplit(first.headers["location"]).query)["state"][0]
    response = client.get(
        "/auth/github/callback",
        params={"code": "test-oauth-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert "location" not in response.headers


@pytest.fixture
def management_client(
    integration_api_client: TestClient,
    github_http_responses: list[httpx2.Response | Exception],
    user_assignments: dict[str, UUID],
) -> TestClient:
    """Authenticate a real management session using only a fake GitHub HTTP peer."""
    _login(integration_api_client)
    return integration_api_client


def _future_expiration() -> str:
    return (datetime.now(UTC) + timedelta(days=7)).isoformat()


def _create(
    client: TestClient, assignment_id: UUID, expires_at: str | None = None
) -> dict[str, object]:
    response = client.post(
        "/tokens",
        json={
            "name": "Notebook",
            "assignment_id": str(assignment_id),
            "expires_at": expires_at or _future_expiration(),
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_callback_stores_digest_only_session_and_cookie_policy(
    integration_api_client: TestClient,
    github_http_responses: list[httpx2.Response | Exception],
    user_assignments: dict[str, UUID],
    session_factory: sessionmaker[Session],
) -> None:
    """OAuth sets a 15-minute HttpOnly management cookie and clears its state cookie."""
    first = integration_api_client.get("/auth/github/login", follow_redirects=False)
    state = parse_qs(urlsplit(first.headers["location"]).query)["state"][0]
    response = integration_api_client.get(
        "/auth/github/callback",
        params={"code": "test-oauth-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert response.content == b""
    assert "location" not in response.headers
    raw = integration_api_client.cookies["sab_token_management_session"]
    assert raw.startswith("sab_session_")
    assert "sab_oauth_state" not in integration_api_client.cookies
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(
        cookie
        for cookie in cookies
        if cookie.startswith("sab_token_management_session=")
    )
    assert "HttpOnly" in session_cookie and "SameSite=lax" in session_cookie
    assert "Path=/" in session_cookie and "Max-Age=900" in session_cookie
    assert "Secure" not in session_cookie
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    with session_factory() as session:
        record = session.scalars(select(TokenManagementSessionRecord)).one()
        assert record.digest == hashlib.sha256(raw.encode()).hexdigest()
        assert record.user_id == user_assignments["user_id"]
        assert record.expires_at - record.created_at == timedelta(minutes=15)
        assert session.scalar(select(func.count()).select_from(ApiTokenRecord)) == 0


def test_contexts_and_token_creation_are_owned_and_keep_secret_out_of_history(
    management_client: TestClient,
    user_assignments: dict[str, UUID],
    session_factory: sessionmaker[Session],
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """One selected owned context issues a bearer credential shown only on creation."""
    contexts = management_client.get("/tokens/contexts").json()["items"]
    assert {(item["role"], item["scope"], item["group_id"]) for item in contexts} == {
        ("edit", "group", "MGI"),
        ("read", "global", None),
    }
    assert str(user_assignments["other_assignment_id"]) not in json.dumps(contexts)
    assert all(
        set(item) == {"assignment_id", "role", "scope", "group_id"} for item in contexts
    )
    result = _create(management_client, user_assignments["assignment_id"])
    raw = str(result["token"])
    assert raw.startswith("sab_") and not raw.startswith("sab_session_")
    token_id = UUID(str(result["token_id"]))
    with session_factory() as session:
        token = session.get(ApiTokenRecord, token_id)
        assert token is not None
        digest = token.digest
        assert digest == hashlib.sha256(raw.encode()).hexdigest()
        assert token.user_id == user_assignments["user_id"]
        assert token.assignment_id == user_assignments["assignment_id"]
        assert token.expires_at == datetime.fromisoformat(
            str(result["expires_at"]).replace("Z", "+00:00")
        )
        stored = session.execute(
            text("SELECT row_to_json(api_token) FROM api_token")
        ).scalar_one()
        assert raw not in json.dumps(stored)
        event = session.scalars(
            select(AuditEventRecord).where(AuditEventRecord.action == "token.created")
        ).one()
        assert event.actor_id == str(user_assignments["user_id"])
        assert event.details["token_id"] == str(token_id)
        assert set(event.details) == {"token_id", "assignment_id", "expires_at"}
        assert raw not in json.dumps(event.details) and digest not in json.dumps(
            event.details
        )
    listing = management_client.get("/tokens")
    assert listing.status_code == status.HTTP_200_OK
    assert raw not in listing.text and digest not in listing.text
    assert (
        "token" not in listing.json()["items"][0]
        and "digest" not in listing.json()["items"][0]
    )
    assert listing.json()["items"][0]["token_id"] == str(token_id)
    with unit_of_work_factory() as uow:
        assert uow.auth.get_active_token(digest, now=datetime.now(UTC)) is not None
        assert (
            uow.auth.get_active_management_session(digest, now=datetime.now(UTC))
            is None
        )
        session_digest = hashlib.sha256(
            management_client.cookies["sab_token_management_session"].encode()
        ).hexdigest()
        assert uow.auth.get_active_token(session_digest, now=datetime.now(UTC)) is None


def test_api_accepts_timezone_aware_expiration_and_normalizes_utc(
    management_client: TestClient,
    user_assignments: dict[str, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token creation accepts one aware timestamp and returns it normalized to UTC."""
    monkeypatch.setattr(
        "standard_annotation_backend.services.token_service._utc_now",
        lambda: datetime(2026, 9, 16, tzinfo=UTC),
    )
    result = _create(
        management_client,
        user_assignments["assignment_id"],
        "2026-10-01T00:00:00-07:00",
    )

    assert result["expires_at"] == "2026-10-01T07:00:00Z"


@pytest.mark.parametrize(
    "expires_at",
    [
        "2027-09-16T00:00:00.000001Z",
        "2026-09-16T00:00:00Z",
        "2026-09-15T00:00:00Z",
        "2026-10-01T00:00:00",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
    ],
)
def test_invalid_expiration_never_persists_token_or_audit(
    management_client: TestClient,
    user_assignments: dict[str, UUID],
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    expires_at: str,
) -> None:
    """Invalid expiration boundaries return a stable validation envelope before writes."""
    monkeypatch.setattr(
        "standard_annotation_backend.services.token_service._utc_now",
        lambda: datetime(2026, 9, 16, tzinfo=UTC),
    )
    response = management_client.post(
        "/tokens",
        json={
            "name": "Notebook",
            "assignment_id": str(user_assignments["assignment_id"]),
            "expires_at": expires_at,
        },
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] in {
        "invalid_token",
        "request_validation_error",
    }
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ApiTokenRecord)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEventRecord)) == 0


def test_cross_user_assignment_and_tokens_return_nondisclosing_404(
    management_client: TestClient,
    user_assignments: dict[str, UUID],
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A management session cannot create or discover another user's credentials."""
    now = datetime.now(UTC)
    with unit_of_work_factory() as uow:
        other_token = uow.auth.create_token(
            user_id=user_assignments["other_user_id"],
            assignment_id=user_assignments["other_assignment_id"],
            name="Other",
            digest="f" * 64,
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        other_token_id = other_token.token_id
        uow.commit()
    response = management_client.post(
        "/tokens",
        json={
            "name": "Intrusion",
            "assignment_id": str(user_assignments["other_assignment_id"]),
            "expires_at": _future_expiration(),
        },
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["error"]["code"] == "token_context_not_found"
    assert management_client.get("/tokens").json() == {"items": []}
    other = management_client.delete(f"/tokens/{other_token_id}")
    unknown = management_client.delete(f"/tokens/{uuid4()}")
    assert other.status_code == unknown.status_code == status.HTTP_404_NOT_FOUND
    assert other.json() == unknown.json()
    assert other.json()["error"]["code"] == "token_not_found"


def test_revocation_preserves_metadata_and_removes_bearer_authentication(
    management_client: TestClient,
    user_assignments: dict[str, UUID],
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Own-token revocation is durable, retains history, and records the user actor."""
    result = _create(management_client, user_assignments["assignment_id"])
    response = management_client.delete(f"/tokens/{result['token_id']}")
    assert response.status_code == status.HTTP_204_NO_CONTENT and not response.content
    metadata = management_client.get("/tokens").json()["items"][0]
    assert metadata["revoked_at"] is not None
    assert metadata["token_id"] == result["token_id"]
    digest = hashlib.sha256(str(result["token"]).encode()).hexdigest()
    with unit_of_work_factory() as uow:
        assert uow.auth.get_active_token(digest, now=datetime.now(UTC)) is None
    with session_factory() as session:
        event = session.scalars(
            select(AuditEventRecord).where(AuditEventRecord.action == "token.revoked")
        ).one()
        assert event.actor_id == str(user_assignments["user_id"])
        assert event.details == {"token_id": result["token_id"]}


@pytest.mark.parametrize(
    "session_state", ["missing", "unknown", "expired", "revoked", "inactive"]
)
def test_management_requires_a_current_cookie_session(
    management_client: TestClient,
    session_factory: sessionmaker[Session],
    session_state: str,
) -> None:
    """Every management operation rejects absent, stale, revoked, or inactive sessions."""
    if session_state == "missing":
        management_client.cookies.clear()
    elif session_state == "unknown":
        management_client.cookies.clear()
        management_client.cookies.set(
            "sab_token_management_session", "sab_session_unknown"
        )
    else:
        with session_factory() as session:
            record = session.scalars(select(TokenManagementSessionRecord)).one()
            if session_state == "expired":
                record.created_at = datetime.now(UTC) - timedelta(hours=1)
                record.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            elif session_state == "revoked":
                record.revoked_at = datetime.now(UTC)
            else:
                record.owner.is_active = False
            session.commit()
    for path in ("/tokens", "/tokens/contexts"):
        response = management_client.get(path)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert response.json()["error"]["code"] == "management_session_required"


def test_unsynchronized_user_cannot_start_management_session(
    integration_api_client: TestClient,
    github_http_responses: list[httpx2.Response | Exception],
    session_factory: sessionmaker[Session],
) -> None:
    """Verified GitHub identity requires an active synchronized SAB user."""
    first = integration_api_client.get("/auth/github/login", follow_redirects=False)
    state = parse_qs(urlsplit(first.headers["location"]).query)["state"][0]
    response = integration_api_client.get(
        "/auth/github/callback",
        params={"code": "test-oauth-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["error"]["code"] == "github_identity_not_allowed"
    assert "sab_oauth_state" not in integration_api_client.cookies
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(TokenManagementSessionRecord)
            )
            == 0
        )


def test_users_yaml_login_can_start_management_without_a_numeric_id(
    integration_api_client: TestClient,
    github_http_responses: list[httpx2.Response | Exception],
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """The accepted upstream schema authorizes an OAuth user's current login."""
    source = """
- accounts: {github: curator}
  authorizations: {sab: [{role: edit, scope: self, group: MGI}]}
"""
    AuthorizationSyncService(unit_of_work_factory).synchronize(
        source, "test/repo", "a" * 40
    )
    _login(integration_api_client)
    assert len(integration_api_client.get("/tokens/contexts").json()["items"]) == 1
    with unit_of_work_factory() as uow:
        owner = uow.auth.get_user_by_github_login("curator")
        assert owner is not None
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(TokenManagementSessionRecord)
            )
            == 1
        )
        assert session.scalar(select(func.count()).select_from(ApiTokenRecord)) == 0


def test_changed_login_gets_new_identity_and_cannot_inherit_old_tokens(
    integration_api_client: TestClient,
    github_http_responses: list[httpx2.Response | Exception],
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A renamed login requires a new grant and cannot inherit the old identity."""
    service = AuthorizationSyncService(unit_of_work_factory)
    source = """
- accounts: {github: curator}
  authorizations: {sab: [{role: edit, scope: self, group: MGI}]}
"""
    service.synchronize(source, "test/repo", "a" * 40)
    responses = list(github_http_responses)
    _login(integration_api_client)
    context = integration_api_client.get("/tokens/contexts").json()["items"][0]
    created = _create(integration_api_client, UUID(context["assignment_id"]))
    with unit_of_work_factory() as uow:
        original = uow.auth.get_user_by_github_login("curator")
        assert original is not None
        original_id = original.user_id

    service.synchronize(
        source.replace("github: curator", "github: renamed-curator"),
        "test/repo",
        "b" * 40,
    )
    integration_api_client.cookies.clear()
    github_http_responses[:] = responses
    first = integration_api_client.get("/auth/github/login", follow_redirects=False)
    state = parse_qs(urlsplit(first.headers["location"]).query)["state"][0]
    old = integration_api_client.get(
        "/auth/github/callback",
        params={"code": "test-oauth-code", "state": state},
        follow_redirects=False,
    )
    assert old.status_code == status.HTTP_403_FORBIDDEN
    assert "sab_token_management_session" not in integration_api_client.cookies

    profile = responses[1]
    assert isinstance(profile, httpx2.Response)
    responses[1] = httpx2.Response(
        200, json=profile.json() | {"login": "Renamed-Curator"}
    )
    github_http_responses[:] = responses
    _login(integration_api_client)
    renamed_context = integration_api_client.get("/tokens/contexts").json()["items"][0]
    assert renamed_context["assignment_id"] != context["assignment_id"]
    assert integration_api_client.get("/tokens").json()["items"] == []
    with pytest.raises(AuthenticationRequiredError):
        AuthenticationService(unit_of_work_factory).authenticate(str(created["token"]))
    _create(integration_api_client, UUID(renamed_context["assignment_id"]))
    with unit_of_work_factory() as uow:
        renamed = uow.auth.get_user_by_github_login("renamed-curator")
        assert renamed is not None and renamed.user_id != original_id
        assert uow.auth.get_user_by_github_login("curator") is None
        renamed_id = renamed.user_id
    with session_factory() as session:
        sessions = session.scalars(select(TokenManagementSessionRecord)).all()
        assert len(sessions) == 2
        assert {item.user_id for item in sessions} == {original_id, renamed_id}


@pytest.mark.parametrize("changed_field", ["role", "scope", "group"])
def test_listing_keeps_issued_context_when_changed_assignment_rejects_authentication(
    management_client: TestClient,
    user_assignments: dict[str, UUID],
    session_factory: sessionmaker[Session],
    unit_of_work_factory: UnitOfWorkFactory,
    changed_field: str,
) -> None:
    """Token history retains its original selection after a current grant changes."""
    created = _create(management_client, user_assignments["assignment_id"])
    authentication = AuthenticationService(unit_of_work_factory)
    raw_token = str(created["token"])
    assert authentication.authenticate(raw_token).actor_id == str(
        user_assignments["user_id"]
    )
    with session_factory.begin() as session:
        assignment = session.get(
            AuthorizationAssignmentRecord, user_assignments["assignment_id"]
        )
        assert assignment is not None
        if changed_field == "role":
            assignment.role = "admin"
        elif changed_field == "scope":
            assignment.scope = "self"
        else:
            group = SabGroupRecord(group_key="RGD")
            session.add(group)
            session.flush()
            assignment.group_id = group.group_id
    item = management_client.get("/tokens").json()["items"][0]
    assert item["token_id"] == created["token_id"]
    assert (item["role"], item["scope"], item["group_id"]) == ("edit", "group", "MGI")
    with pytest.raises(AuthenticationRequiredError):
        authentication.authenticate(raw_token)


@pytest.mark.parametrize("operation", ["create", "revoke"])
def test_token_change_rolls_back_if_audit_cannot_be_written(
    management_client: TestClient,
    user_assignments: dict[str, UUID],
    session_factory: sessionmaker[Session],
    operation: str,
) -> None:
    """A database audit rejection rolls the token mutation back in the shared transaction."""
    token_id = None
    if operation == "revoke":
        token_id = UUID(
            str(
                _create(management_client, user_assignments["assignment_id"])[
                    "token_id"
                ]
            )
        )
    action = "token.created" if operation == "create" else "token.revoked"
    with session_factory() as session:
        session.execute(
            text(
                "ALTER TABLE audit_event ADD CONSTRAINT task4_reject_audit CHECK (action != '"
                + action
                + "')"
            )
        )
        session.commit()
    try:
        if operation == "create":
            response = management_client.post(
                "/tokens",
                json={
                    "name": "Notebook",
                    "assignment_id": str(user_assignments["assignment_id"]),
                    "expires_at": _future_expiration(),
                },
            )
        else:
            response = management_client.delete(f"/tokens/{token_id}")
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"]["code"] == "credential_storage_unavailable"
        with session_factory() as session:
            if token_id is None:
                assert (
                    session.scalar(select(func.count()).select_from(ApiTokenRecord))
                    == 0
                )
            else:
                token = session.get(ApiTokenRecord, token_id)
                assert token is not None and token.revoked_at is None
    finally:
        with session_factory() as session:
            session.execute(
                text("ALTER TABLE audit_event DROP CONSTRAINT task4_reject_audit")
            )
            session.commit()


@pytest.mark.parametrize("credential", ["token", "session"])
def test_persistence_failures_never_expose_credential_digests(
    user_assignments: dict[str, UUID],
    unit_of_work_factory: UnitOfWorkFactory,
    credential: str,
) -> None:
    """Credential uniqueness failures do not leak digests through exception logging."""
    digest = hashlib.sha256(b"task4-only-test-credential").hexdigest()
    now = datetime.now(UTC)
    with unit_of_work_factory() as uow:
        with pytest.raises(Exception) as raised:
            for _ in range(2):
                if credential == "token":
                    uow.auth.create_token(
                        user_id=user_assignments["user_id"],
                        assignment_id=user_assignments["assignment_id"],
                        name="Notebook",
                        digest=digest,
                        created_at=now,
                        expires_at=now + timedelta(days=7),
                    )
                else:
                    uow.auth.create_management_session(
                        user_id=user_assignments["user_id"],
                        digest=digest,
                        created_at=now,
                        expires_at=now + timedelta(minutes=15),
                    )
        rendered = "".join(traceback.format_exception(raised.value))
        assert digest not in rendered
        assert str(raised.value) == "Credential storage is unavailable"


def test_production_management_session_cookie_is_secure(
    integration_api_client: TestClient,
    github_http_responses: list[httpx2.Response | Exception],
    user_assignments: dict[str, UUID],
) -> None:
    """Successful deployed callbacks issue Secure cookies with the same narrow lifetime."""
    # The fixture validates its registered development redirect URI at the HTTP
    # boundary; switch the cookie environment only after constructing that client.
    from standard_annotation_backend.api.routes.tokens import get_github_oauth_client
    from standard_annotation_backend.auth.github_oauth import GitHubOAuthClient

    github = GitHubOAuthClient(app.state.settings)
    app.dependency_overrides[get_github_oauth_client] = lambda: github
    first = integration_api_client.get("/auth/github/login", follow_redirects=False)
    state = parse_qs(urlsplit(first.headers["location"]).query)["state"][0]
    app.state.settings.environment = "production"
    response = integration_api_client.get(
        "/auth/github/callback",
        params={"state": state, "code": "test-oauth-code"},
        follow_redirects=False,
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert all("Secure" in cookie for cookie in response.headers.get_list("set-cookie"))


def test_bearer_header_cannot_authenticate_token_management(
    management_client: TestClient,
    user_assignments: dict[str, UUID],
) -> None:
    """A valid bearer token does not grant authority to manage credentials."""
    created = _create(management_client, user_assignments["assignment_id"])
    management_client.cookies.clear()
    response = management_client.get(
        "/tokens", headers={"Authorization": f"Bearer {created['token']}"}
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    management_client.cookies.set("sab_token_management_session", str(created["token"]))
    assert (
        management_client.get("/tokens/contexts").status_code
        == status.HTTP_401_UNAUTHORIZED
    )


def test_removed_assignment_disappears_from_choices_and_cannot_issue_new_token(
    management_client: TestClient,
    user_assignments: dict[str, UUID],
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Authorization synchronization affects existing management sessions immediately."""
    created = _create(management_client, user_assignments["assignment_id"])
    with unit_of_work_factory() as uow:
        uow.auth.replace_authorizations(
            users=(SyncUser("curator", "Curator"),),
            source_repository="test/repo",
            source_commit_sha="b" * 40,
            summary={},
        )
        uow.commit()
    assert management_client.get("/tokens/contexts").json() == {"items": []}
    items = management_client.get("/tokens").json()["items"]
    assert items[0]["token_id"] == created["token_id"]
    assert items[0]["assignment_is_active"] is False
    response = management_client.post(
        "/tokens",
        json={
            "name": "Another",
            "assignment_id": str(user_assignments["assignment_id"]),
            "expires_at": _future_expiration(),
        },
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.parametrize("name", ["", "  ", "x" * 201])
def test_creation_service_validates_names_without_an_http_boundary(
    management_client: TestClient,
    user_assignments: dict[str, UUID],
    unit_of_work_factory: UnitOfWorkFactory,
    name: str,
) -> None:
    """Non-HTTP callers cannot issue blank or overlong names through the service."""
    service = TokenService(unit_of_work_factory)
    raw = management_client.cookies["sab_token_management_session"]
    with pytest.raises(InvalidTokenError):
        service.create_token(
            raw,
            {
                "name": name,
                "assignment_id": str(user_assignments["assignment_id"]),
                "expires_at": _future_expiration(),
            },
        )
    assert service.list_tokens(raw) == ()


def test_credential_storage_failure_returns_safe_api_error(
    management_client: TestClient,
    user_assignments: dict[str, UUID],
    session_factory: sessionmaker[Session],
) -> None:
    """A credential write failure returns a stable error and leaves no partial token."""
    with session_factory() as session:
        session.execute(
            text(
                "ALTER TABLE api_token ADD CONSTRAINT task4_reject_token CHECK (false)"
            )
        )
        session.commit()
    try:
        response = management_client.post(
            "/tokens",
            json={
                "name": "Notebook",
                "assignment_id": str(user_assignments["assignment_id"]),
                "expires_at": _future_expiration(),
            },
        )
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"]["code"] == "credential_storage_unavailable"
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(ApiTokenRecord)) == 0
            assert (
                session.scalar(select(func.count()).select_from(AuditEventRecord)) == 0
            )
    finally:
        with session_factory() as session:
            session.execute(
                text("ALTER TABLE api_token DROP CONSTRAINT task4_reject_token")
            )
            session.commit()


@pytest.mark.parametrize("operation", ["revoke", "record_use"])
def test_credential_update_failures_never_disclose_unchanged_digests(
    user_assignments: dict[str, UUID],
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    operation: str,
) -> None:
    """Failed token updates hide the complete failing row reported by PostgreSQL."""
    digest = hashlib.sha256(b"task4-test-update-credential").hexdigest()
    now = datetime.now(UTC)
    with unit_of_work_factory() as uow:
        token = uow.auth.create_token(
            user_id=user_assignments["user_id"],
            assignment_id=user_assignments["assignment_id"],
            name="Notebook",
            digest=digest,
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        token_id = token.token_id
        uow.commit()
    with session_factory() as session:
        session.execute(
            text(
                "ALTER TABLE api_token ADD CONSTRAINT task4_reject_update CHECK (revoked_at IS NULL AND last_used_at IS NULL)"
            )
        )
        session.commit()
    try:
        with unit_of_work_factory() as uow, pytest.raises(Exception) as raised:
            if operation == "revoke":
                uow.auth.revoke_token(
                    user_assignments["user_id"], token_id, revoked_at=now
                )
            else:
                uow.auth.record_token_use(token_id, used_at=now)
        assert digest not in "".join(traceback.format_exception(raised.value))
        assert str(raised.value) == "Credential storage is unavailable"
    finally:
        with session_factory() as session:
            session.execute(
                text("ALTER TABLE api_token DROP CONSTRAINT task4_reject_update")
            )
            session.commit()


@pytest.mark.parametrize("failure_stage", ["lookup", "commit"])
def test_callback_database_failures_clear_state_and_return_safe_error(
    integration_api_client: TestClient,
    github_http_responses: list[httpx2.Response | Exception],
    user_assignments: dict[str, UUID],
    session_factory: sessionmaker[Session],
    failure_stage: str,
) -> None:
    """Lookup and deferred commit failures return a private response and consume OAuth state."""
    first = integration_api_client.get("/auth/github/login", follow_redirects=False)
    state = parse_qs(urlsplit(first.headers["location"]).query)["state"][0]
    with session_factory() as session:
        if failure_stage == "lookup":
            session.execute(
                text("ALTER TABLE sab_user RENAME TO task4_unavailable_sab_user")
            )
        else:
            session.execute(
                text("""
                CREATE FUNCTION task4_reject_management_commit()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'test-only credential failure: %', NEW.digest;
                    RETURN NEW;
                END;
                $$
            """)
            )
            session.execute(
                text("""
                CREATE CONSTRAINT TRIGGER task4_reject_management_commit
                AFTER INSERT ON token_management_session
                DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION task4_reject_management_commit()
            """)
            )
        session.commit()
    try:
        response = integration_api_client.get(
            "/auth/github/callback",
            params={"code": "test-oauth-code", "state": state},
            follow_redirects=False,
        )
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json() == {
            "error": {
                "code": "credential_storage_unavailable",
                "message": "Credential storage is unavailable",
            }
        }
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "sab_oauth_state" not in integration_api_client.cookies
        assert "sab_token_management_session" not in integration_api_client.cookies
        assert state not in response.text
        assert "test-oauth-code" not in response.text
        assert "digest" not in response.text
    finally:
        with session_factory() as session:
            if failure_stage == "lookup":
                session.execute(
                    text("ALTER TABLE task4_unavailable_sab_user RENAME TO sab_user")
                )
            else:
                session.execute(
                    text(
                        "DROP TRIGGER task4_reject_management_commit ON token_management_session"
                    )
                )
                session.execute(text("DROP FUNCTION task4_reject_management_commit()"))
            session.commit()
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(TokenManagementSessionRecord)
            )
            == 0
        )


def test_unexpected_callback_failure_also_clears_state_and_returns_safe_error(
    integration_api_client: TestClient,
    github_http_responses: list[httpx2.Response | Exception],
) -> None:
    """An unexpected callback exception still returns a private, non-disclosing error."""
    first = integration_api_client.get("/auth/github/login", follow_redirects=False)
    state = parse_qs(urlsplit(first.headers["location"]).query)["state"][0]
    github_http_responses[:] = [
        RuntimeError("test-oauth-code test-client-secret " + state)
    ]
    response = integration_api_client.get(
        "/auth/github/callback",
        params={"code": "test-oauth-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json() == {
        "error": {
            "code": "oauth_callback_failed",
            "message": "GitHub authentication could not be completed",
        }
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "sab_oauth_state" not in integration_api_client.cookies
    assert "sab_token_management_session" not in integration_api_client.cookies
    assert "test-client-secret" not in response.text and state not in response.text
