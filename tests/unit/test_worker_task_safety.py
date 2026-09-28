"""Verify that Celery retries omit infrastructure error details."""

import traceback
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from celery.exceptions import Retry

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.workers import tasks

CANARY = "raw-database-credential-diagnostic"


def _safe_retry(monkeypatch: pytest.MonkeyPatch, task: object) -> None:
    def retry(*, exc: Exception, countdown: int) -> None:
        assert countdown == 30
        raise Retry(exc=exc)

    monkeypatch.setattr(task, "retry", retry)


def _assert_safe_retry(caught: pytest.ExceptionInfo[Retry], logs: str) -> None:
    rendered = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert CANARY not in rendered
    assert CANARY not in logs
    assert "could not persist state" in rendered


def test_runtime_startup_failure_retries_without_diagnostic_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Database setup failure requests a retry without its error details."""

    @contextmanager
    def unavailable_runtime():
        raise RuntimeError(CANARY)
        yield

    monkeypatch.setattr(tasks, "worker_runtime", unavailable_runtime)
    _safe_retry(monkeypatch, tasks.run_authorization_sync)

    with pytest.raises(Retry) as caught:
        tasks.run_authorization_sync.run(str(uuid4()))

    _assert_safe_retry(caught, caplog.text)


def test_failed_failure_persistence_retries_without_diagnostic_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A retry exposes neither the job error nor the database error details."""
    job_id = uuid4()

    class Jobs:
        def start(self, _job_id: object) -> object:
            return SimpleNamespace(
                job_id=job_id,
                job_type=JobType.AUTHORIZATION_SYNC,
                status=JobStatus.RUNNING,
                requested_by="scheduler",
                parameters={
                    "source_repository": "geneontology/go-site",
                    "source_ref": "master",
                    "source_path": "metadata/users.yaml",
                },
            )

        def fail(self, _job_id: object, *, error: str) -> None:
            raise RuntimeError(CANARY)

    @contextmanager
    def runtime():
        yield SimpleNamespace(engine=object(), jobs=Jobs())

    @contextmanager
    def lock(_engine: object, _job_id: object):
        yield True

    def source_failure(_self: object) -> object:
        raise RuntimeError("untrusted-upstream-response")

    monkeypatch.setattr(tasks, "worker_runtime", runtime)
    monkeypatch.setattr(tasks, "job_execution_lock", lock)
    monkeypatch.setattr(tasks.AuthorizationSourceClient, "fetch", source_failure)
    _safe_retry(monkeypatch, tasks.run_authorization_sync)

    with pytest.raises(Retry) as caught:
        tasks.run_authorization_sync.run(str(job_id))

    _assert_safe_retry(caught, caplog.text)
    assert "untrusted-upstream-response" not in caplog.text
