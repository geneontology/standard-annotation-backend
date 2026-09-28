"""Verify that authorization sync jobs store their state in PostgreSQL."""

import os
from collections.abc import Iterator
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.config import get_settings
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    AuthorizationSyncRecord,
    JobRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.authorization_sync_service import (
    AuthorizationSyncService,
)
from standard_annotation_backend.services.job_service import JobService
from standard_annotation_backend.workers.tasks import (
    run_authorization_sync,
    schedule_authorization_sync,
)

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
    return (
        JobService(factory)
        .create(
            job_type=JobType.AUTHORIZATION_SYNC,
            requested_by="scheduler",
            parameters={
                "source_repository": "geneontology/go-site",
                "source_ref": "master",
                "source_path": "metadata/users.yaml",
            },
        )
        .job_id
    )


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


def test_authorization_sync_job_succeeds_with_durable_result(
    worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker records the source commit, counts, and successful result."""
    job_id = _create_job(unit_of_work_factory)
    _install_github(monkeypatch, [(SHA, VALID_SOURCE.encode())])

    run_authorization_sync.run(str(job_id))

    job = _load_job(unit_of_work_factory, job_id)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.result is not None
    UUID(str(job.result["sync_id"]))
    assert job.result == {
        "sync_id": job.result["sync_id"],
        "source_repository": "geneontology/go-site",
        "source_commit_sha": SHA,
        "user_count": 1,
        "group_count": 1,
        "assignment_count": 1,
        "applied": True,
    }
    assert job.error is None


@pytest.mark.parametrize(
    "source",
    [httpx2.Response(503, content=b"upstream secret"), (SHA, b"[")],
)
def test_authorization_sync_job_failure_is_safe_and_preserves_prior_state(
    worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    source: tuple[str, bytes] | httpx2.Response,
) -> None:
    """Source and validation failures store one public error and no auth changes."""
    prior = AuthorizationSyncService(unit_of_work_factory).synchronize(
        VALID_SOURCE,
        "geneontology/go-site",
        "b" * 40,
    )
    job_id = _create_job(unit_of_work_factory)
    requests = _install_github(monkeypatch, [source])

    run_authorization_sync.run(str(job_id))

    job = _load_job(unit_of_work_factory, job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.error == "Authorization synchronization failed"
    assert "secret" not in job.error
    with session_factory() as session:
        syncs = session.scalars(select(AuthorizationSyncRecord)).all()
        assert len(syncs) == 1
        assert syncs[0].sync_id == prior.sync_id
    assert requests


def test_redelivery_after_success_does_not_repeat_external_or_audit_work(
    worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redelivery of a completed job performs no source request or audit write."""
    job_id = _create_job(unit_of_work_factory)
    requests = _install_github(monkeypatch, [(SHA, VALID_SOURCE.encode())])
    run_authorization_sync.run(str(job_id))
    with session_factory() as session:
        before = session.scalar(select(func.count()).select_from(AuditEventRecord))

    run_authorization_sync.run(str(job_id))

    assert len(requests) == 2
    with session_factory() as session:
        after = session.scalar(select(func.count()).select_from(AuditEventRecord))
    assert after == before


def test_redelivery_after_post_sync_crash_reuses_provenance(
    worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after sync commit resumes the job without replacing grants again."""
    job_id = _create_job(unit_of_work_factory)
    requests = _install_github(
        monkeypatch,
        [
            httpx2.Response(200, json={"sha": SHA}),
            httpx2.Response(200, content=VALID_SOURCE.encode()),
            httpx2.Response(200, content=VALID_SOURCE.encode()),
        ],
    )
    original_succeed = JobService.succeed

    class WorkerCrash(BaseException):
        pass

    def crash_after_sync(self: JobService, *args: object, **kwargs: object):
        raise WorkerCrash

    monkeypatch.setattr(JobService, "succeed", crash_after_sync)
    with pytest.raises(WorkerCrash):
        run_authorization_sync.run(str(job_id))
    crashed = _load_job(unit_of_work_factory, job_id)
    assert crashed.status == JobStatus.RUNNING.value
    assert crashed.parameters["source_commit_sha"] == SHA
    monkeypatch.setattr(JobService, "succeed", original_succeed)

    run_authorization_sync.run(str(job_id))

    job = _load_job(unit_of_work_factory, job_id)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.result is not None and job.result["applied"] is False
    assert len(requests) == 3
    assert "/contents/metadata/users.yaml" in str(requests[-1].url)
    assert requests[-1].url.params["ref"] == SHA
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(AuthorizationSyncRecord))
            == 1
        )
        sync_audits = session.scalar(
            select(func.count())
            .select_from(AuditEventRecord)
            .where(
                AuditEventRecord.action == AuditAction.AUTHORIZATION_SYNCHRONIZED.value
            )
        )
        assert sync_audits == 1


def test_two_jobs_for_same_sha_share_one_applied_sync(
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

    run_authorization_sync.run(str(first_id))
    run_authorization_sync.run(str(second_id))

    first = _load_job(unit_of_work_factory, first_id)
    second = _load_job(unit_of_work_factory, second_id)
    assert first.status == second.status == JobStatus.SUCCEEDED.value
    assert first.result is not None and second.result is not None
    assert first.result["sync_id"] == second.result["sync_id"]
    assert {first.result["applied"], second.result["applied"]} == {True, False}
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(AuthorizationSyncRecord))
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

    monkeypatch.setattr(run_authorization_sync, "delay", fail_dispatch)

    schedule_authorization_sync.run()

    assert len(dispatched) == 1
    job = _load_job(unit_of_work_factory, UUID(dispatched[0]))
    assert job.requested_by == "scheduler"
    assert job.parameters == {
        "source_repository": "geneontology/go-site",
        "source_ref": "master",
        "source_path": "metadata/users.yaml",
    }
    assert job.status == JobStatus.FAILED.value
    assert job.error == "Authorization synchronization could not be dispatched"
