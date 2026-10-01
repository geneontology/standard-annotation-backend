"""Verify creation, reuse, dispatch, and scheduling of entity import runs."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from test_entity_import_service import stage

from standard_annotation_backend.config import GpiEntitySourceSettings
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.entity_sources.registry import EntitySourceRegistry
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.entity_import_run_service import (
    EntityImportRunService,
)
from standard_annotation_backend.services.entity_import_service import (
    EntityImportService,
)
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.workers import tasks

REGISTRY = EntitySourceRegistry(
    {
        "mgi": GpiEntitySourceSettings(url="https://example.org/mgi.gpi"),
        "rgd": GpiEntitySourceSettings(url="https://example.org/rgd.gpi"),
    }
)


def _service(
    unit_of_work_factory: UnitOfWorkFactory,
    dispatched: list[Job],
    registry: EntitySourceRegistry = REGISTRY,
) -> EntityImportRunService:
    return EntityImportRunService(unit_of_work_factory, registry, dispatched.append)


def _kinds(jobs: tuple[Job, ...]) -> list[tuple[JobType, str]]:
    return [(job.job_type, str(job.parameters["source_key"])) for job in jobs]


def test_import_all_creates_imports_and_retirements(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Every configured source is imported and every unconfigured catalog retired."""
    imports = EntityImportService(unit_of_work_factory)
    imports.publish(
        job_id=stage(imports, session_factory, "ZFIN:1", source="zfin"),
        actor_id="curator",
    )
    dispatched: list[Job] = []

    jobs = _service(unit_of_work_factory, dispatched).start(
        requested_by="scheduler", source_key=None
    )

    assert _kinds(jobs) == [
        (JobType.ENTITY_CATALOG_RETIREMENT, "zfin"),
        (JobType.ENTITY_IMPORT, "mgi"),
        (JobType.ENTITY_IMPORT, "rgd"),
    ]
    assert all(job.status is JobStatus.QUEUED for job in jobs)
    assert [job.job_id for job in dispatched] == [job.job_id for job in jobs]


@pytest.mark.parametrize("starting_status", [JobStatus.QUEUED, JobStatus.RUNNING])
def test_unfinished_jobs_are_reused_and_dispatched_again(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    starting_status: JobStatus,
) -> None:
    """A queued or running job for the same source is reused and sent again.

    Sending a running job again lets a job whose worker was lost resume.
    """
    first = _service(unit_of_work_factory, []).start(
        requested_by="admin", source_key="mgi"
    )
    if starting_status is JobStatus.RUNNING:
        JobService(unit_of_work_factory).start(first[0].job_id)
    dispatched: list[Job] = []

    second = _service(unit_of_work_factory, dispatched).start(
        requested_by="scheduler", source_key="mgi"
    )

    assert [job.job_id for job in second] == [first[0].job_id]
    assert second[0].status is starting_status
    assert [job.job_id for job in dispatched] == [first[0].job_id]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 1


def test_new_job_dispatch_failure_fails_only_that_job(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """If sending a new job to the task queue fails, only that job is marked failed."""

    def dispatch(job: Job) -> None:
        if job.parameters["source_key"] == "mgi":
            raise ConnectionError("broker down")

    jobs = EntityImportRunService(unit_of_work_factory, REGISTRY, dispatch).start(
        requested_by="scheduler", source_key=None
    )

    statuses = {str(job.parameters["source_key"]): job.status for job in jobs}
    assert statuses == {"mgi": JobStatus.FAILED, "rgd": JobStatus.QUEUED}


def test_concurrent_starts_create_one_job_per_source(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Independent connections starting at once cannot duplicate jobs."""
    barrier = Barrier(2)

    def start() -> tuple[Job, ...]:
        barrier.wait()
        return _service(unit_of_work_factory, []).start(
            requested_by="scheduler", source_key=None
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(lambda _: start(), range(2))

    assert {job.job_id for job in first} == {job.job_id for job in second}
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 2


def test_scheduled_import_creates_scheduler_jobs_for_every_source(
    monkeypatch: pytest.MonkeyPatch,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """The scheduled task queues and dispatches an import for every configured source.

    The jobs are requested by `scheduler`.
    """
    imported: list[str] = []
    retired: list[str] = []

    @contextmanager
    def runtime() -> Iterator[object]:
        yield SimpleNamespace(
            unit_of_work_factory=unit_of_work_factory, entity_sources=REGISTRY
        )

    monkeypatch.setattr(tasks, "worker_runtime", runtime)
    monkeypatch.setattr(tasks.run_entity_import, "delay", imported.append)
    monkeypatch.setattr(tasks.run_entity_catalog_retirement, "delay", retired.append)

    tasks.schedule_entity_imports.run()

    with session_factory() as session:
        records = list(session.scalars(select(JobRecord)))
        jobs = sorted(
            (
                record.job_type,
                record.status,
                record.requested_by,
                record.parameters["source_key"],
            )
            for record in records
        )
        job_ids = sorted(str(record.job_id) for record in records)
    assert jobs == [
        ("entity_import", "queued", "scheduler", "mgi"),
        ("entity_import", "queued", "scheduler", "rgd"),
    ]
    assert sorted(imported) == job_ids
    assert retired == []
