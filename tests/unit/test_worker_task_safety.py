"""Verify that Celery retries omit infrastructure error details."""

import traceback
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from celery import Task
from celery.exceptions import Retry

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.workers import tasks

CANARY = "raw-database-credential-diagnostic"


def _safe_retry(monkeypatch: pytest.MonkeyPatch, task: object) -> None:
    def retry(*, exc: Exception, countdown: int) -> None:
        assert countdown == 30
        raise Retry(exc=exc)

    monkeypatch.setattr(task, "retry", retry)


def _enable_task_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-enable the task logger so its records reach `caplog`.

    Alembic's logging setup, which integration tests run earlier in the same
    process, disables loggers that already exist, including this one.
    """
    monkeypatch.setattr(tasks.logger, "disabled", False)


def _assert_safe_retry(caught: pytest.ExceptionInfo[Retry], logs: str) -> None:
    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert CANARY not in rendered
    assert CANARY not in logs
    assert "could not persist state" in rendered
    assert "failure_type=RuntimeError" in logs


@pytest.mark.parametrize(
    ("task", "arguments"),
    [
        (tasks.run_refresh, (str(uuid4()),)),
        (tasks.run_entity_retirement, (str(uuid4()),)),
        (tasks.schedule_refresh, ("entity",)),
    ],
    ids=[
        "refresh",
        "entity_retirement",
        "refresh_schedule",
    ],
)
def test_runtime_startup_failure_retries_without_diagnostic_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    task: Task,
    arguments: tuple[str, ...],
) -> None:
    """Database setup failure requests a retry without its error details."""

    @contextmanager
    def unavailable_runtime(**_kwargs: object):
        raise RuntimeError(CANARY)
        yield

    monkeypatch.setattr(tasks, "worker_runtime", unavailable_runtime)
    _safe_retry(monkeypatch, task)
    _enable_task_logs(monkeypatch)

    with pytest.raises(Retry) as caught:
        task.run(*arguments)

    _assert_safe_retry(caught, caplog.text)


@pytest.mark.parametrize(
    ("task", "method"),
    [(tasks.run_refresh, "run"), (tasks.run_entity_retirement, "run_retirement")],
    ids=["refresh", "entity_retirement"],
)
def test_runner_failure_retries_without_diagnostic_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    task: Task,
    method: str,
) -> None:
    """An unexpected runner error requests a retry without its error details."""

    def fail(_job_id: UUID) -> None:
        raise RuntimeError(CANARY)

    @contextmanager
    def runtime(**_kwargs: object):
        yield SimpleNamespace(runner=SimpleNamespace(**{method: fail}))

    monkeypatch.setattr(tasks, "worker_runtime", runtime)
    _safe_retry(monkeypatch, task)
    _enable_task_logs(monkeypatch)

    with pytest.raises(Retry) as caught:
        task.run(str(uuid4()))

    _assert_safe_retry(caught, caplog.text)


def _job(job_id: UUID, job_type: JobType) -> Job:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    return Job(
        job_id=job_id,
        job_type=job_type,
        status=JobStatus.QUEUED,
        requested_by="scheduler",
        parameters={"source_key": "go"},
        progress={},
        warnings=(),
        result=None,
        artifact_uri=None,
        error=None,
        created_at=now,
        updated_at=now,
        started_at=None,
        completed_at=None,
    )


def test_dispatcher_routes_retirement_and_refresh_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retirement jobs go to the retirement task; every other job to `sab.refresh.run`."""
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tasks.run_refresh, "delay", lambda job_id: sent.append(("refresh", job_id))
    )
    monkeypatch.setattr(
        tasks.run_entity_retirement,
        "delay",
        lambda job_id: sent.append(("retirement", job_id)),
    )

    tasks.dispatch_refresh_job(_job(UUID(int=1), JobType.ONTOLOGY_REFRESH))
    tasks.dispatch_refresh_job(_job(UUID(int=2), JobType.ENTITY_RETIREMENT))

    assert sent == [("refresh", str(UUID(int=1))), ("retirement", str(UUID(int=2)))]


def test_schedule_ignores_unknown_kind_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown kind in a Beat entry is logged once and not retried forever."""

    def retry(**_kwargs: object) -> None:
        pytest.fail("an unknown kind must not be retried")

    monkeypatch.setattr(tasks.schedule_refresh, "retry", retry)
    _enable_task_logs(monkeypatch)

    tasks.schedule_refresh.run("bogus")

    assert "unknown kind" in caplog.text
