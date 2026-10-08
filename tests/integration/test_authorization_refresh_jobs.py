"""Verify that authorization refresh jobs store their state in PostgreSQL."""

import os
from collections.abc import Iterator
from hashlib import sha256
from uuid import UUID

import httpx2
import pytest
from celery.exceptions import Retry
from refresh_helpers import apply_users_yaml
from seeding import create_job
from source_provenance import github_provenance
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.config import get_settings
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    AuthorizationRefreshRecord,
    JobRecord,
)
from standard_annotation_backend.persistence.repositories.auth import AuthRepository
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.job_service import JobService
from standard_annotation_backend.workers import tasks

VALID_SOURCE = """
- accounts: {github: curator}
  authorizations:
    sab: [{role: edit, scope: group, group: MGI}]
"""
SHA = "a" * 40


@pytest.fixture
def worker_environment(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Point worker-created engines at the guarded disposable test database."""
    monkeypatch.setenv("SAB_DATABASE_URL", os.environ["SAB_TEST_DATABASE_URL"])
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _create_job(factory: UnitOfWorkFactory) -> UUID:
    return create_job(
        factory,
        job_type=JobType.AUTHORIZATION_REFRESH,
        requested_by="scheduler",
        parameters={"source_key": "go-site"},
    ).job_id


def _load_job(factory: UnitOfWorkFactory, job_id: UUID) -> JobRecord:
    with factory() as unit_of_work:
        record = unit_of_work.jobs.get(job_id)
        assert record is not None
        unit_of_work.jobs.session.expunge(record)
        return record


def _install_github(
    monkeypatch: pytest.MonkeyPatch,
    sources: list[tuple[str, bytes] | httpx2.Response],
) -> list[httpx2.Request]:
    responses: list[httpx2.Response] = []
    for source in sources:
        if isinstance(source, httpx2.Response):
            responses.append(source)
        else:
            sha, content = source
            responses.extend(
                [
                    httpx2.Response(200, json={"sha": sha}),
                    httpx2.Response(200, content=content),
                ]
            )
    requests: list[httpx2.Request] = []

    def handle_request(
        _transport: httpx2.HTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
        requests.append(request)
        response = responses.pop(0)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", handle_request)
    return requests


def test_authorization_refresh_job_succeeds_with_durable_result(
    worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker records the source commit, counts, and successful result."""
    job_id = _create_job(unit_of_work_factory)
    requests = _install_github(monkeypatch, [(SHA, VALID_SOURCE.encode())])

    tasks.run_refresh.run(str(job_id))

    job = _load_job(unit_of_work_factory, job_id)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.result is not None
    UUID(str(job.result["refresh_id"]))
    assert job.result == {
        "refresh_id": job.result["refresh_id"],
        "source_type": "github",
        "source_locator": "github:geneontology/go-site:metadata/users.yaml",
        "source_revision": SHA,
        "source_checksum": sha256(VALID_SOURCE.encode()).hexdigest(),
        "fetched_at": job.result["fetched_at"],
        "refreshed_at": job.result["refreshed_at"],
        "user_count": 1,
        "group_count": 1,
        "assignment_count": 1,
        "unchanged": False,
    }
    assert job.error is None
    assert job.parameters == {"source_key": "go-site"}
    # The commit is resolved first, then the file is read at that commit.
    assert len(requests) == 2
    assert requests[1].url.host == "raw.githubusercontent.com"
    assert SHA in str(requests[1].url)


@pytest.mark.parametrize(
    ("source", "failure_code"),
    [
        (httpx2.Response(503, content=b"upstream secret"), "source_error"),
        ((SHA, b"["), "invalid_document"),
    ],
)
def test_authorization_refresh_failure_is_safe_and_preserves_prior_state(
    worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    source: tuple[str, bytes] | httpx2.Response,
    failure_code: str,
) -> None:
    """Source and validation failures store one public error and no auth changes."""
    prior = apply_users_yaml(
        unit_of_work_factory, VALID_SOURCE, github_provenance("b" * 40)
    )
    job_id = _create_job(unit_of_work_factory)
    _install_github(monkeypatch, [source])

    tasks.run_refresh.run(str(job_id))

    job = _load_job(unit_of_work_factory, job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.error == "Authorization refresh failed"
    assert "secret" not in job.error
    assert job.progress["failure_code"] == failure_code
    with session_factory() as session:
        refreshes = session.scalars(select(AuthorizationRefreshRecord)).all()
        assert len(refreshes) == 1
        assert str(refreshes[0].refresh_id) == prior.result["refresh_id"]


def test_infrastructure_failure_requests_retry(
    worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected error asks Celery to retry and leaves the job running."""
    job_id = _create_job(unit_of_work_factory)
    _install_github(monkeypatch, [(SHA, VALID_SOURCE.encode())])

    def lose_database(*_args: object, **_kwargs: object) -> None:
        raise ConnectionResetError("database lost")

    monkeypatch.setattr(AuthRepository, "replace_authorizations", lose_database)
    # Calling `.run` outside a worker has no broker, so replace the retry call
    # with one that raises `Retry` as a worker would.
    monkeypatch.setattr(
        tasks.run_refresh, "retry", lambda **_kwargs: (_ for _ in ()).throw(Retry())
    )

    with pytest.raises(Retry):
        tasks.run_refresh.run(str(job_id))

    assert _load_job(unit_of_work_factory, job_id).status == JobStatus.RUNNING.value


def test_redelivery_after_success_does_not_repeat_audit_work(
    worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redelivery of a completed job performs no source request or audit write."""
    job_id = _create_job(unit_of_work_factory)
    requests = _install_github(monkeypatch, [(SHA, VALID_SOURCE.encode())])
    tasks.run_refresh.run(str(job_id))
    with session_factory() as session:
        before = session.scalar(select(func.count()).select_from(AuditEventRecord))

    tasks.run_refresh.run(str(job_id))

    assert len(requests) == 2
    with session_factory() as session:
        after = session.scalar(select(func.count()).select_from(AuditEventRecord))
    assert after == before


def test_redelivery_after_committed_refresh_fetches_again_and_applies_nothing(
    worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the refresh commits resumes the job without replacing grants."""
    job_id = _create_job(unit_of_work_factory)
    requests = _install_github(
        monkeypatch,
        [(SHA, VALID_SOURCE.encode()), (SHA, VALID_SOURCE.encode())],
    )
    original_succeed = JobService.succeed

    class WorkerCrash(BaseException):
        pass

    def crash_after_refresh(self: JobService, *args: object, **kwargs: object):
        raise WorkerCrash

    monkeypatch.setattr(JobService, "succeed", crash_after_refresh)
    with pytest.raises(WorkerCrash):
        tasks.run_refresh.run(str(job_id))
    crashed = _load_job(unit_of_work_factory, job_id)
    assert crashed.status == JobStatus.RUNNING.value
    monkeypatch.setattr(JobService, "succeed", original_succeed)

    tasks.run_refresh.run(str(job_id))

    job = _load_job(unit_of_work_factory, job_id)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.result is not None and job.result["unchanged"] is True
    assert job.parameters == {"source_key": "go-site"}
    # Each attempt resolves the commit and downloads the file again.
    assert len(requests) == 4
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(AuthorizationRefreshRecord))
            == 1
        )
        refreshed_audits = session.scalar(
            select(func.count())
            .select_from(AuditEventRecord)
            .where(AuditEventRecord.action == AuditAction.AUTHORIZATION_REFRESHED.value)
        )
        assert refreshed_audits == 1


def test_two_jobs_for_same_sha_share_one_applied_refresh(
    worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct jobs may resolve one revision while only one replaces authorizations."""
    first_id = _create_job(unit_of_work_factory)
    second_id = _create_job(unit_of_work_factory)
    _install_github(
        monkeypatch,
        [(SHA, VALID_SOURCE.encode()), (SHA, VALID_SOURCE.encode())],
    )

    tasks.run_refresh.run(str(first_id))
    tasks.run_refresh.run(str(second_id))

    first = _load_job(unit_of_work_factory, first_id)
    second = _load_job(unit_of_work_factory, second_id)
    assert first.status == second.status == JobStatus.SUCCEEDED.value
    assert first.result is not None and second.result is not None
    assert first.result["refresh_id"] == second.result["refresh_id"]
    assert first.result["unchanged"] is False
    assert second.result["unchanged"] is True
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(AuthorizationRefreshRecord))
            == 1
        )


def test_scheduler_creates_job_before_dispatch_and_records_publication_failure(
    worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch failure marks the previously created scheduled job as failed."""
    dispatched: list[str] = []

    def fail_dispatch(job_id: str) -> None:
        dispatched.append(job_id)
        raise RuntimeError("redis credential diagnostic")

    monkeypatch.setattr(tasks.run_refresh, "delay", fail_dispatch)

    tasks.schedule_refresh.run("authorization")

    assert len(dispatched) == 1
    job = _load_job(unit_of_work_factory, UUID(dispatched[0]))
    assert job.requested_by == "scheduler"
    assert job.parameters == {"source_key": "go-site"}
    assert job.status == JobStatus.FAILED.value
    assert job.error == "Authorization refresh could not be dispatched"
