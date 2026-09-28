"""Test creation of stored ontology-load jobs through the API."""

from uuid import UUID

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from standard_annotation_backend.api.dependencies import get_authenticated_context
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
    RequestContext,
)
from standard_annotation_backend.main import app
from standard_annotation_backend.workers import tasks


def _context(role: AuthorizationRole, scope: AuthorizationScope) -> RequestContext:
    return RequestContext(
        actor_id="api-test-user",
        token_id=UUID("00000000-0000-0000-0000-000000000501"),
        token_name="Integration tests",
        role=role,
        scope=scope,
        group_id=None if scope is AuthorizationScope.GLOBAL else "group-1",
    )


def test_admin_global_request_creates_job_without_exposing_parameters(
    integration_api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The response reports a stored queued job and omits worker parameters."""
    dispatched: list[str] = []
    monkeypatch.setattr(tasks.run_ontology_load, "delay", dispatched.append)

    response = integration_api_client.post("/ontology-loads", json={"ontology": "go"})

    assert response.status_code == status.HTTP_202_ACCEPTED
    body = response.json()
    assert body["job_type"] == "ontology_load"
    assert body["status"] == "queued"
    assert "parameters" not in body
    assert dispatched == [body["job_id"]]


@pytest.mark.parametrize(
    "payload",
    [
        {"ontology": "unknown"},
        {"ontology": "go", "source_url": "https://example.test/go.obo"},
        {},
    ],
)
def test_request_body_rejects_unknown_keys_and_client_source_configuration(
    integration_api_client: TestClient,
    payload: dict[str, object],
) -> None:
    """The API accepts one supported key and no client-supplied source settings."""
    response = integration_api_client.post("/ontology-loads", json=payload)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"


@pytest.mark.parametrize(
    ("role", "scope"),
    [
        (AuthorizationRole.READ, AuthorizationScope.GLOBAL),
        (AuthorizationRole.EDIT, AuthorizationScope.GLOBAL),
        (AuthorizationRole.ADMIN, AuthorizationScope.SELF),
        (AuthorizationRole.ADMIN, AuthorizationScope.GROUP),
    ],
)
def test_only_admin_global_context_can_create_load(
    integration_api_client: TestClient,
    role: AuthorizationRole,
    scope: AuthorizationScope,
) -> None:
    """Only a global administrator can create ontology-load jobs."""
    previous = app.dependency_overrides[get_authenticated_context]
    app.dependency_overrides[get_authenticated_context] = lambda: _context(role, scope)
    try:
        response = integration_api_client.post(
            "/ontology-loads", json={"ontology": "go"}
        )
    finally:
        app.dependency_overrides[get_authenticated_context] = previous

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["error"]["code"] == "permission_denied"


def test_missing_and_invalid_bearer_are_rejected(
    integration_api_client: TestClient,
) -> None:
    """The route requires the same bearer authentication as other API routes."""
    previous = app.dependency_overrides.pop(get_authenticated_context)
    try:
        missing = integration_api_client.post(
            "/ontology-loads", json={"ontology": "go"}
        )
        invalid = integration_api_client.post(
            "/ontology-loads",
            json={"ontology": "go"},
            headers={"Authorization": "Bearer invalid"},
        )
    finally:
        app.dependency_overrides[get_authenticated_context] = previous

    assert missing.status_code == status.HTTP_401_UNAUTHORIZED
    assert invalid.status_code == status.HTTP_401_UNAUTHORIZED


def test_dispatch_failure_returns_terminal_job_resource(
    integration_api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch failure is stored without changing the accepted response."""
    monkeypatch.setattr(
        tasks.run_ontology_load,
        "delay",
        lambda _job_id: (_ for _ in ()).throw(RuntimeError("broker detail")),
    )

    response = integration_api_client.post("/ontology-loads", json={"ontology": "go"})

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == "Ontology load could not be dispatched"
    assert "broker detail" not in response.text
