"""Verify that Celery retries omit infrastructure error details."""

import traceback
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from celery import Task
from celery.exceptions import Retry

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
        (tasks.run_job, (str(uuid4()),)),
        (tasks.schedule_refresh, ("entity",)),
    ],
    ids=["job", "refresh_schedule"],
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


def test_runner_failure_retries_without_diagnostic_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected runner error requests a retry without its error details."""

    def fail(_job_id: UUID) -> None:
        raise RuntimeError(CANARY)

    @contextmanager
    def runtime(**_kwargs: object):
        yield SimpleNamespace(executors=SimpleNamespace(run=fail))

    monkeypatch.setattr(tasks, "worker_runtime", runtime)
    _safe_retry(monkeypatch, tasks.run_job)
    _enable_task_logs(monkeypatch)

    with pytest.raises(Retry) as caught:
        tasks.run_job.run(str(uuid4()))

    _assert_safe_retry(caught, caplog.text)


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
