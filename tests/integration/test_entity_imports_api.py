"""Test creation of entity import jobs through the API."""

from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from test_entity_import_service import stage

from standard_annotation_backend.api.dependencies import (
    get_authenticated_context,
    get_entity_source_registry,
)
from standard_annotation_backend.config import GpiEntitySourceSettings
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
    RequestContext,
)
from standard_annotation_backend.entity_sources.registry import EntitySourceRegistry
from standard_annotation_backend.main import app
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.entity_import_service import (
    EntityImportService,
)
from standard_annotation_backend.workers import tasks

REGISTRY = EntitySourceRegistry(
    {
        "mgi": GpiEntitySourceSettings(url="https://example.org/mgi.gpi.gz"),
        "rgd": GpiEntitySourceSettings(url="https://example.org/rgd.gpi.gz"),
    }
)


@pytest.fixture(autouse=True)
def configured_registry() -> Iterator[None]:
    """Serve a fixed registry and restore dependency overrides afterward."""
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_entity_source_registry] = lambda: REGISTRY
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture
def set_context() -> Iterator[None]:
    """Restore the integration client's authentication override after each test."""
    previous = app.dependency_overrides[get_authenticated_context]
    try:
        yield
    finally:
        app.dependency_overrides[get_authenticated_context] = previous


def _authenticate(role: AuthorizationRole, scope: AuthorizationScope) -> None:
    context = RequestContext(
        actor_id="api-test-user",
        token_id=UUID("00000000-0000-0000-0000-000000000501"),
        token_name="Entity import API tests",
        role=role,
        scope=scope,
        group_id=None if scope is AuthorizationScope.GLOBAL else "MGI",
    )
    app.dependency_overrides[get_authenticated_context] = lambda: context


def test_global_admin_imports_one_configured_source(
    integration_api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creating one import stores only the source key and dispatches the job ID.

    The response contains a queued job without its parameters.
    """
    dispatched: list[str] = []
    monkeypatch.setattr(tasks.run_entity_import, "delay", dispatched.append)

    response = integration_api_client.post(
        "/entity-imports", json={"source_key": "mgi"}
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    jobs = response.json()["jobs"]
    assert [job["job_type"] for job in jobs] == ["entity_import"]
    assert jobs[0]["status"] == "queued"
    assert "parameters" not in jobs[0]
    assert dispatched == [jobs[0]["job_id"]]
    with session_factory() as session:
        record = session.get(JobRecord, UUID(jobs[0]["job_id"]))
        assert record is not None
        assert record.parameters == {"source_key": "mgi"}


def test_unconfigured_source_is_rejected_without_creating_a_job(
    integration_api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-formed source key that is not configured is rejected and creates no job.

    The response uses the `unknown_entity_source` error code.
    """
    monkeypatch.setattr(tasks.run_entity_import, "delay", lambda _job_id: None)

    response = integration_api_client.post(
        "/entity-imports", json={"source_key": "zfin"}
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    error = response.json()["error"]
    assert error["code"] == "unknown_entity_source"
    assert error["details"][0]["location"] == ["source_key"]
    with session_factory() as session:
        assert session.query(JobRecord).count() == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"source_key": "MGI"},
        {"source_key": ""},
        {"source_key": "mgi", "source_url": "https://example.org/x.gpi"},
        {"source_key": 7},
    ],
)
def test_malformed_requests_are_rejected(
    integration_api_client: TestClient, payload: dict[str, object]
) -> None:
    """Malformed keys and extra fields fail request validation."""
    response = integration_api_client.post("/entity-imports", json=payload)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize(
    ("role", "scope"),
    [
        (AuthorizationRole.ADMIN, AuthorizationScope.GROUP),
        (AuthorizationRole.ADMIN, AuthorizationScope.SELF),
        (AuthorizationRole.EDIT, AuthorizationScope.GLOBAL),
        (AuthorizationRole.READ, AuthorizationScope.GLOBAL),
    ],
)
def test_only_global_admins_can_create_entity_imports(
    integration_api_client: TestClient,
    role: AuthorizationRole,
    scope: AuthorizationScope,
) -> None:
    """Every non-global-admin context is forbidden."""
    _authenticate(role, scope)
    response = integration_api_client.post(
        "/entity-imports", json={"source_key": "mgi"}
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid"}])
def test_entity_import_requires_valid_bearer_authentication(
    integration_api_client: TestClient,
    set_context: None,
    headers: dict[str, str],
) -> None:
    """Missing and invalid bearer credentials cannot create an import job."""
    app.dependency_overrides.pop(get_authenticated_context)

    response = integration_api_client.post(
        "/entity-imports", json={"source_key": "mgi"}, headers=headers
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_dispatch_failure_is_durable(
    integration_api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the job cannot be sent to the broker, it is returned as failed.

    The error message is fixed and does not expose broker connection details.
    """

    def fail(_job_id: str) -> None:
        raise ConnectionError("redis://user:secret@broker")

    monkeypatch.setattr(tasks.run_entity_import, "delay", fail)

    response = integration_api_client.post(
        "/entity-imports", json={"source_key": "mgi"}
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    job = response.json()["jobs"][0]
    assert job["status"] == "failed"
    assert job["error"] == "Entity import could not be dispatched"
    assert "secret" not in response.text


def test_global_admin_imports_all_configured_sources(
    integration_api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty request imports every configured source."""
    dispatched: list[str] = []
    monkeypatch.setattr(tasks.run_entity_import, "delay", dispatched.append)

    response = integration_api_client.post("/entity-imports", json={})

    assert response.status_code == status.HTTP_202_ACCEPTED
    jobs = response.json()["jobs"]
    assert [job["job_type"] for job in jobs] == ["entity_import", "entity_import"]
    assert dispatched == [job["job_id"] for job in jobs]


def test_import_all_with_empty_registry_returns_no_jobs(
    integration_api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing configured and nothing active yields an empty job list."""
    app.dependency_overrides[get_entity_source_registry] = lambda: EntitySourceRegistry(
        {}
    )
    monkeypatch.setattr(tasks.run_entity_import, "delay", lambda _job_id: None)

    response = integration_api_client.post("/entity-imports", json={})

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.json() == {"jobs": []}


def test_import_all_routes_each_job_type_to_its_own_task(
    integration_api_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty request sends import jobs and retirement jobs to separate tasks.

    Retirement jobs cover catalogs whose source was removed from the registry.
    """
    imports = EntityImportService(unit_of_work_factory)
    imports.publish(
        job_id=stage(imports, session_factory, "ZFIN:1", source="zfin"),
        actor_id="curator",
    )
    import_ids: list[str] = []
    retirement_ids: list[str] = []
    monkeypatch.setattr(tasks.run_entity_import, "delay", import_ids.append)
    monkeypatch.setattr(
        tasks.run_entity_catalog_retirement, "delay", retirement_ids.append
    )

    response = integration_api_client.post("/entity-imports", json={})

    assert response.status_code == status.HTTP_202_ACCEPTED
    by_type: dict[str, list[str]] = {}
    for job in response.json()["jobs"]:
        by_type.setdefault(job["job_type"], []).append(job["job_id"])
    assert retirement_ids == by_type["entity_catalog_retirement"]
    assert len(retirement_ids) == 1
    assert import_ids == by_type["entity_import"]
    assert len(import_ids) == 2
