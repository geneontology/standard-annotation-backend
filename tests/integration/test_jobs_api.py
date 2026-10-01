"""Verify authenticated job status reads through the real database and API."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.api.dependencies import get_authenticated_context
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
    RequestContext,
)
from standard_annotation_backend.main import app
from standard_annotation_backend.persistence.models import JobRecord

OWN_JOB_ID = UUID("00000000-0000-0000-0000-000000000901")
OTHER_JOB_ID = UUID("00000000-0000-0000-0000-000000000902")
FOREIGN_JOB_ID = UUID("00000000-0000-0000-0000-000000000903")
SCHEDULER_JOB_ID = UUID("00000000-0000-0000-0000-000000000904")
MGI_ENTITY_IMPORT_JOB_ID = UUID("00000000-0000-0000-0000-000000000906")
UNKNOWN_JOB_ID = UUID("00000000-0000-0000-0000-000000000999")
CREATED_AT = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
JOB_MISSING = {"error": {"code": "job_not_found", "message": "Job was not found"}}


def _context(
    actor_id: str,
    role: AuthorizationRole,
    scope: AuthorizationScope,
    group_id: str | None,
) -> RequestContext:
    """Build an authenticated context for one role and scope."""
    return RequestContext(
        actor_id=actor_id,
        token_id=UUID("00000000-0000-0000-0000-000000000905"),
        token_name="Job status tests",
        role=role,
        scope=scope,
        group_id=group_id,
    )


@pytest.fixture
def seeded_jobs(session_factory: sessionmaker[Session]) -> None:
    """Persist jobs requested by a user and the scheduler."""
    with session_factory.begin() as session:
        for job_id, requester in (
            (OWN_JOB_ID, "actor-a"),
            (OTHER_JOB_ID, "actor-b"),
            (FOREIGN_JOB_ID, "actor-a"),
            (SCHEDULER_JOB_ID, "scheduler"),
        ):
            session.add(
                JobRecord(
                    job_id=job_id,
                    job_type="authorization_sync",
                    status="succeeded",
                    requested_by=requester,
                    parameters={"source_token": "must-not-be-public"},
                    progress={"processed": 3, "total": 3},
                    warnings=["One unknown team was ignored"],
                    result={"applied": True},
                    artifact_uri="s3://sab-results/job.json",
                    error=None,
                    created_at=CREATED_AT,
                    updated_at=CREATED_AT + timedelta(minutes=2),
                    started_at=CREATED_AT + timedelta(minutes=1),
                    completed_at=CREATED_AT + timedelta(minutes=2),
                )
            )
        session.add(
            JobRecord(
                job_id=MGI_ENTITY_IMPORT_JOB_ID,
                job_type="entity_import",
                status="succeeded",
                requested_by="entity-import-operator",
                parameters={"source_key": "mgi"},
                progress={"phase": "completed"},
                warnings=[],
                result={"source_key": "mgi"},
                artifact_uri=None,
                error=None,
                created_at=CREATED_AT,
                updated_at=CREATED_AT + timedelta(minutes=2),
                started_at=CREATED_AT + timedelta(minutes=1),
                completed_at=CREATED_AT + timedelta(minutes=2),
            )
        )


@pytest.fixture
def set_context() -> Iterator[None]:
    """Restore the integration client's authentication override after each test."""
    previous = app.dependency_overrides[get_authenticated_context]
    try:
        yield
    finally:
        app.dependency_overrides[get_authenticated_context] = previous


def _authenticate(context: RequestContext) -> None:
    app.dependency_overrides[get_authenticated_context] = lambda: context


def test_global_admin_reads_job_with_safe_complete_response(
    integration_api_client: TestClient,
    seeded_jobs: None,
    set_context: None,
) -> None:
    """A global admin receives public job fields without worker parameters."""
    _authenticate(
        _context("operator", AuthorizationRole.ADMIN, AuthorizationScope.GLOBAL, None)
    )

    response = integration_api_client.get(f"/jobs/{OWN_JOB_ID}")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "job_id": str(OWN_JOB_ID),
        "job_type": "authorization_sync",
        "status": "succeeded",
        "requested_by": "actor-a",
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
    assert "parameters" not in response.json()
    assert "source_token" not in response.text


@pytest.mark.parametrize("job_id", [OWN_JOB_ID, UNKNOWN_JOB_ID])
@pytest.mark.parametrize(
    "context",
    [
        _context("reader", AuthorizationRole.READ, AuthorizationScope.GLOBAL, None),
        _context("editor", AuthorizationRole.EDIT, AuthorizationScope.GLOBAL, None),
        _context("admin", AuthorizationRole.ADMIN, AuthorizationScope.SELF, "MGI"),
        _context("admin", AuthorizationRole.ADMIN, AuthorizationScope.GROUP, "MGI"),
        _context("reader", AuthorizationRole.READ, AuthorizationScope.GROUP, "MGI"),
        _context("editor", AuthorizationRole.EDIT, AuthorizationScope.GROUP, "MGI"),
    ],
)
def test_job_status_requires_global_admin_authorization(
    integration_api_client: TestClient,
    seeded_jobs: None,
    set_context: None,
    context: RequestContext,
    job_id: UUID,
) -> None:
    """Neither global scope nor the admin role grants job access by itself.

    Callers without access receive 403 whether or not the job exists, so the
    response does not reveal which job IDs exist.
    """
    _authenticate(context)

    response = integration_api_client.get(f"/jobs/{job_id}")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json() == {
        "error": {"code": "permission_denied", "message": "Permission denied"}
    }


@pytest.mark.parametrize(
    "job_id",
    [OTHER_JOB_ID, FOREIGN_JOB_ID, SCHEDULER_JOB_ID, MGI_ENTITY_IMPORT_JOB_ID],
)
def test_global_admin_reads_jobs_from_every_requester(
    integration_api_client: TestClient,
    seeded_jobs: None,
    set_context: None,
    job_id: UUID,
) -> None:
    """Job visibility does not depend on the requesting actor."""
    _authenticate(
        _context("operator", AuthorizationRole.ADMIN, AuthorizationScope.GLOBAL, None)
    )

    response = integration_api_client.get(f"/jobs/{job_id}")

    assert response.status_code == status.HTTP_200_OK


def test_global_admin_receives_not_found_for_unknown_job(
    integration_api_client: TestClient,
    seeded_jobs: None,
    set_context: None,
) -> None:
    """An authorized request for an unknown job returns not found."""
    _authenticate(
        _context("operator", AuthorizationRole.ADMIN, AuthorizationScope.GLOBAL, None)
    )

    response = integration_api_client.get(f"/jobs/{UNKNOWN_JOB_ID}")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == JOB_MISSING


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer sab_token_invalid"}],
)
def test_job_status_requires_valid_bearer_authentication(
    integration_api_client: TestClient,
    seeded_jobs: None,
    set_context: None,
    headers: dict[str, str],
) -> None:
    """A job identifier does not bypass missing or invalid bearer credentials."""
    app.dependency_overrides.pop(get_authenticated_context)

    response = integration_api_client.get(f"/jobs/{OWN_JOB_ID}", headers=headers)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {
        "error": {
            "code": "authentication_required",
            "message": "Authentication required",
        }
    }
