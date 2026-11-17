"""Verify that the `sabe.job.run` task runs annotation scan jobs and ignores others."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from refresh_helpers import FakeFetchers, build_runner
from sqlalchemy import Engine

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_scan import queue_or_join
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.workers import tasks


@pytest.fixture(autouse=True)
def _test_runtime(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Point the Celery task at the production runners on the test database."""

    @contextmanager
    def runtime(
        *, dispatch_follow_up: Callable[[Job], None] | None = None
    ) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(
            executors=build_runner(
                database_engine,
                unit_of_work_factory,
                FakeFetchers({}),
                dispatch_follow_up=dispatch_follow_up,
            )
        )

    monkeypatch.setattr(tasks, "worker_runtime", runtime)


@pytest.mark.parametrize(
    "job_type", [JobType.TERM_REPLACEMENT, JobType.ANNOTATION_QC_REPORT]
)
def test_the_task_runs_annotation_scan_jobs(
    job_type: JobType,
    active_annotation_subjects: None,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A term replacement or QC report job sent to `sabe.job.run` succeeds."""
    with unit_of_work_factory() as uow:
        job = queue_or_join(
            uow, job_type=job_type, group_key=None, requested_by="curator"
        ).job
        uow.commit()

    tasks.run_job.run(str(job.job_id))

    assert JobService(unit_of_work_factory).find(job.job_id).status is (
        JobStatus.SUCCEEDED
    )


def test_a_message_for_an_unknown_job_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message naming a job that does not exist finishes without a retry."""

    def retry(**_kwargs: object) -> None:
        pytest.fail("a message for an unknown job must not be retried")

    monkeypatch.setattr(tasks.run_job, "retry", retry)

    tasks.run_job.run(str(uuid4()))
