"""Verify the global-admin refresh and job routes."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from refresh_helpers import TEST_SOURCES, sources_with_entities
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.api.dependencies import get_authenticated_context
from standard_annotation_backend.api.routes import admin
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
    RequestContext,
)
from standard_annotation_backend.main import app
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.workers import tasks

ROUTES = {
    "/admin/authorization-refreshes": ("authorization_refresh", ["go-site"], "go-site"),
    "/admin/ontology-refreshes": ("ontology_refresh", ["go"], "go"),
    "/admin/entity-refreshes": ("entity_refresh", ["mgi", "rgd"], "rgd"),
}
AUTHORIZATION_ROUTE = "/admin/authorization-refreshes"
SOURCE_KEY_ROUTES = sorted(path for path in ROUTES if path != AUTHORIZATION_ROUTE)


def _refresh_all_body(path: str) -> dict[str, object] | None:
    """Return the body that refreshes every source; authorization takes none."""
    return None if path == AUTHORIZATION_ROUTE else {}


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[Job]:
    """Use test sources and record dispatched jobs instead of using Celery."""
    sent: list[Job] = []
    monkeypatch.setattr(
        admin, "get_settings", lambda: SimpleNamespace(sources=TEST_SOURCES)
    )
    monkeypatch.setattr(tasks, "dispatch_refresh_job", sent.append)
    return sent


def _context(role: AuthorizationRole, scope: AuthorizationScope) -> RequestContext:
    return RequestContext(
        actor_id="someone",
        token_id=UUID("00000000-0000-0000-0000-000000000777"),
        token_name="Denied",
        role=role,
        scope=scope,
        group_id=None if scope is AuthorizationScope.GLOBAL else "MGI",
    )


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_refresh_all_returns_202_with_one_job_per_source(
    integration_api_client: TestClient, dispatched: list[Job], path: str
) -> None:
    """An empty body refreshes every configured source of the route's kind."""
    job_type, keys, _ = ROUTES[path]

    response = integration_api_client.post(path, json=_refresh_all_body(path))

    assert response.status_code == status.HTTP_202_ACCEPTED
    jobs = response.json()["jobs"]
    assert [job["job_type"] for job in jobs] == [job_type] * len(keys)
    assert [job["status"] for job in jobs] == ["queued"] * len(keys)
    assert "parameters" not in jobs[0]
    assert len(dispatched) == len(keys)


@pytest.mark.parametrize("path", SOURCE_KEY_ROUTES)
def test_refresh_one_source_and_reuse_its_unfinished_job(
    integration_api_client: TestClient, dispatched: list[Job], path: str
) -> None:
    """A named source gets one job; asking again returns the same job."""
    _, _, key = ROUTES[path]

    first = integration_api_client.post(path, json={"source_key": key})
    second = integration_api_client.post(path, json={"source_key": key})

    assert first.status_code == second.status_code == status.HTTP_202_ACCEPTED
    assert [job["job_id"] for job in first.json()["jobs"]] == [
        job["job_id"] for job in second.json()["jobs"]
    ]


@pytest.mark.parametrize("path", SOURCE_KEY_ROUTES)
def test_unknown_source_is_422_unknown_source(
    integration_api_client: TestClient,
    dispatched: list[Job],
    session_factory: sessionmaker[Session],
    path: str,
) -> None:
    """An unconfigured key is reported at `source_key` and creates no job."""
    response = integration_api_client.post(path, json={"source_key": "missing"})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    error = response.json()["error"]
    assert error["code"] == "unknown_source"
    assert error["details"][0]["location"] == ["source_key"]
    assert error["details"][0]["type"] == "unknown_source"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 0


@pytest.mark.parametrize("path", SOURCE_KEY_ROUTES)
@pytest.mark.parametrize(
    "body", [{"source_key": "go", "extra": 1}, {"source_key": "Bad"}]
)
def test_invalid_bodies_are_rejected(
    integration_api_client: TestClient,
    dispatched: list[Job],
    path: str,
    body: dict[str, object],
) -> None:
    """Unexpected fields and malformed keys fail request validation."""
    response = integration_api_client.post(path, json=body)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_authorization_refresh_needs_no_body_and_reuses_its_unfinished_job(
    integration_api_client: TestClient, dispatched: list[Job]
) -> None:
    """The single authorization source is refreshed without naming it."""
    first = integration_api_client.post(AUTHORIZATION_ROUTE)
    second = integration_api_client.post(AUTHORIZATION_ROUTE)

    assert first.status_code == second.status_code == status.HTTP_202_ACCEPTED
    [job] = first.json()["jobs"]
    assert job["job_type"] == "authorization_refresh"
    assert [job["job_id"] for job in second.json()["jobs"]] == [job["job_id"]]
    assert [sent.parameters for sent in dispatched] == [
        {"source_key": "go-site"},
        {"source_key": "go-site"},
    ]


DENIED = [
    (AuthorizationRole.ADMIN, AuthorizationScope.GROUP),
    (AuthorizationRole.EDIT, AuthorizationScope.GLOBAL),
    (AuthorizationRole.READ, AuthorizationScope.GLOBAL),
    (AuthorizationRole.EDIT, AuthorizationScope.GROUP),
    (AuthorizationRole.EDIT, AuthorizationScope.SELF),
]


@pytest.mark.parametrize("path", sorted(ROUTES))
@pytest.mark.parametrize(("role", "scope"), DENIED)
def test_only_global_admins_can_refresh_and_denial_precedes_lookup(
    integration_api_client: TestClient,
    dispatched: list[Job],
    session_factory: sessionmaker[Session],
    path: str,
    role: AuthorizationRole,
    scope: AuthorizationScope,
) -> None:
    """Every other context gets 403, even for an unknown key, and no job."""
    app.dependency_overrides[get_authenticated_context] = lambda: _context(role, scope)

    body = None if path == AUTHORIZATION_ROUTE else {"source_key": "missing"}

    response = integration_api_client.post(path, json=body)

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert dispatched == []
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 0


def test_global_admin_reads_a_job_and_others_cannot(
    integration_api_client: TestClient, dispatched: list[Job]
) -> None:
    """Job status is readable only with the admin role and global scope."""
    created = integration_api_client.post(
        "/admin/ontology-refreshes", json={"source_key": "go"}
    ).json()["jobs"][0]

    found = integration_api_client.get(f"/admin/jobs/{created['job_id']}")
    missing = integration_api_client.get(f"/admin/jobs/{uuid4()}")
    app.dependency_overrides[get_authenticated_context] = lambda: _context(
        AuthorizationRole.ADMIN, AuthorizationScope.GROUP
    )
    denied = integration_api_client.get(f"/admin/jobs/{created['job_id']}")

    assert found.status_code == status.HTTP_200_OK
    assert found.json()["job_id"] == created["job_id"]
    assert missing.status_code == status.HTTP_404_NOT_FOUND
    assert missing.json()["error"]["code"] == "job_not_found"
    assert denied.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_dispatch_failure_returns_failed_job_without_broker_details(
    integration_api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """A job that cannot be sent to the broker is returned as failed."""
    _, _, key = ROUTES[path]
    monkeypatch.setattr(
        admin, "get_settings", lambda: SimpleNamespace(sources=TEST_SOURCES)
    )

    def fail(_job: Job) -> None:
        raise ConnectionError("redis://user:secret@broker")

    monkeypatch.setattr(tasks, "dispatch_refresh_job", fail)

    body = None if path == AUTHORIZATION_ROUTE else {"source_key": key}

    response = integration_api_client.post(path, json=body)

    assert response.status_code == status.HTTP_202_ACCEPTED
    job = response.json()["jobs"][0]
    assert job["status"] == "failed"
    assert job["error"].endswith("refresh could not be dispatched")
    assert "secret" not in response.text


def test_refresh_all_entities_with_no_sources_returns_no_jobs(
    integration_api_client: TestClient,
    dispatched: list[Job],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing configured and nothing active yields an empty job list."""
    monkeypatch.setattr(
        admin,
        "get_settings",
        lambda: SimpleNamespace(sources=sources_with_entities({})),
    )

    response = integration_api_client.post("/admin/entity-refreshes", json={})

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.json() == {"jobs": []}


@pytest.mark.parametrize("path", sorted(ROUTES))
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid"}])
def test_missing_and_invalid_bearer_credentials_are_rejected(
    integration_api_client: TestClient,
    dispatched: list[Job],
    path: str,
    headers: dict[str, str],
) -> None:
    """Refresh routes require bearer authentication."""
    app.dependency_overrides.pop(get_authenticated_context)

    response = integration_api_client.post(
        path, json=_refresh_all_body(path), headers=headers
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert dispatched == []


def test_job_read_returns_public_fields_without_worker_parameters(
    integration_api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """A job from any requester is returned with every public field, no parameters."""
    job_id = UUID("00000000-0000-0000-0000-000000000901")
    created = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        session.add(
            JobRecord(
                job_id=job_id,
                job_type="authorization_refresh",
                status="succeeded",
                requested_by="scheduler",
                parameters={"source_token": "must-not-be-public"},
                progress={"processed": 3, "total": 3},
                warnings=["One unknown team was ignored"],
                result={"applied": True},
                artifact_uri="s3://sab-results/job.json",
                error=None,
                created_at=created,
                updated_at=created + timedelta(minutes=2),
                started_at=created + timedelta(minutes=1),
                completed_at=created + timedelta(minutes=2),
            )
        )

    response = integration_api_client.get(f"/admin/jobs/{job_id}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "job_id": str(job_id),
        "job_type": "authorization_refresh",
        "status": "succeeded",
        "requested_by": "scheduler",
        "progress": {"processed": 3, "total": 3},
        "warnings": ["One unknown team was ignored"],
        "result": {"applied": True},
        "artifact_uri": "s3://sab-results/job.json",
        "error": None,
        "created_at": "2026-09-24T12:00:00Z",
        "updated_at": "2026-09-24T12:02:00Z",
        "started_at": "2026-09-24T12:01:00Z",
        "completed_at": "2026-09-24T12:02:00Z",
    }
    assert "source_token" not in response.text


@pytest.mark.parametrize(("role", "scope"), DENIED)
def test_job_read_denial_does_not_reveal_whether_the_job_exists(
    integration_api_client: TestClient,
    role: AuthorizationRole,
    scope: AuthorizationScope,
) -> None:
    """Callers without access get the same 403 for any job identifier."""
    app.dependency_overrides[get_authenticated_context] = lambda: _context(role, scope)

    response = integration_api_client.get(f"/admin/jobs/{uuid4()}")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json() == {
        "error": {"code": "permission_denied", "message": "Permission denied"}
    }


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid"}])
def test_job_read_requires_valid_bearer_authentication(
    integration_api_client: TestClient, headers: dict[str, str]
) -> None:
    """A job identifier does not bypass missing or invalid credentials."""
    app.dependency_overrides.pop(get_authenticated_context)

    response = integration_api_client.get(f"/admin/jobs/{uuid4()}", headers=headers)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["WWW-Authenticate"] == "Bearer"
