"""Verify creation, reuse, and dispatch of refresh jobs."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace

import pytest
from refresh_helpers import TEST_SOURCES
from seeding import seed_entity_catalog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.auth import system_context
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import RefreshKindName
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.refresh_start_service import (
    RefreshStart,
    RefreshStartService,
)
from standard_annotation_backend.workers import tasks


def _service(
    unit_of_work_factory: UnitOfWorkFactory, dispatched: list[Job]
) -> RefreshStartService:
    return RefreshStartService(unit_of_work_factory, TEST_SOURCES, dispatched.append)


def _targets(jobs: tuple[Job, ...]) -> list[tuple[JobType, str]]:
    return [(job.job_type, str(job.parameters["source_key"])) for job in jobs]


def test_refresh_all_entities_refreshes_every_configured_source(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Every configured source is refreshed and a removed one is retired.

    Only refresh jobs are created, one per configured source.
    """
    seed_entity_catalog(session_factory, ["ZFIN:1"], source_key="zfin")
    dispatched: list[Job] = []

    started = _service(unit_of_work_factory, dispatched).start(
        RefreshKindName.ENTITY, context=system_context("scheduler"), source_key=None
    )

    jobs = started.jobs
    assert _targets(jobs) == [
        (JobType.ENTITY_REFRESH, "mgi"),
        (JobType.ENTITY_REFRESH, "rgd"),
    ]
    assert started.retired == ("zfin",)
    assert all(job.status is JobStatus.QUEUED for job in jobs)
    assert all(job.requested_by == "scheduler" for job in jobs)
    assert [job.job_id for job in dispatched] == [job.job_id for job in jobs]
    assert all(
        job.parameters == {"source_key": key}
        for job, (_, key) in zip(jobs, _targets(jobs), strict=True)
    )


@pytest.mark.parametrize("starting_status", [JobStatus.QUEUED, JobStatus.RUNNING])
def test_unfinished_jobs_are_reused_and_dispatched_again(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    starting_status: JobStatus,
) -> None:
    """A queued or running job for the same source is reused and sent again.

    Sending a running job again lets a job whose worker was lost run again.
    """
    created = _service(unit_of_work_factory, []).start(
        RefreshKindName.ENTITY, context=system_context("admin"), source_key="mgi"
    )
    [first] = created.jobs
    if starting_status is JobStatus.RUNNING:
        JobService(unit_of_work_factory).start(first.job_id)
    dispatched: list[Job] = []

    reused = _service(unit_of_work_factory, dispatched).start(
        RefreshKindName.ENTITY, context=system_context("scheduler"), source_key="mgi"
    )
    [second] = reused.jobs

    assert second.job_id == first.job_id
    assert second.status is starting_status
    assert [job.job_id for job in dispatched] == [first.job_id]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 1


def test_dispatch_failure_fails_only_new_jobs_with_kind_message(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """If sending a new job fails, only that job fails, with a kind-specific error."""

    def dispatch(job: Job) -> None:
        if job.parameters["source_key"] == "mgi":
            raise ConnectionError("broker down")

    started = RefreshStartService(unit_of_work_factory, TEST_SOURCES, dispatch).start(
        RefreshKindName.ENTITY, context=system_context("scheduler"), source_key=None
    )

    by_key = {str(job.parameters["source_key"]): job for job in started.jobs}
    assert by_key["mgi"].status is JobStatus.FAILED
    assert by_key["mgi"].error == "Entity refresh could not be dispatched"
    assert by_key["mgi"].progress == {
        "phase": "failed",
        "failure_code": "dispatch_failed",
    }
    assert by_key["rgd"].status is JobStatus.QUEUED


def test_dispatch_failure_keeps_a_reused_job_unfinished(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A reused job stays queued when sending it again fails.

    An earlier dispatch may already have placed the job in the broker.
    """
    created = _service(unit_of_work_factory, []).start(
        RefreshKindName.ENTITY, context=system_context("admin"), source_key="mgi"
    )
    [first] = created.jobs

    def dispatch(_job: Job) -> None:
        raise ConnectionError("broker down")

    reused = RefreshStartService(unit_of_work_factory, TEST_SOURCES, dispatch).start(
        RefreshKindName.ENTITY, context=system_context("admin"), source_key="mgi"
    )
    [second] = reused.jobs

    assert second.job_id == first.job_id
    assert second.status is JobStatus.QUEUED


def test_concurrent_starts_on_independent_connections_create_one_job_each(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Two simultaneous starts on separate connections cannot duplicate jobs."""
    barrier = Barrier(2)

    def start(_: int) -> RefreshStart:
        barrier.wait()
        return _service(unit_of_work_factory, []).start(
            RefreshKindName.ENTITY, context=system_context("scheduler"), source_key=None
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(start, range(2))

    assert {job.job_id for job in first.jobs} == {job.job_id for job in second.jobs}
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 2


def test_scheduled_refresh_creates_scheduler_jobs_for_every_source(
    monkeypatch: pytest.MonkeyPatch,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """The scheduled task queues and dispatches a refresh of every configured source.

    The jobs are requested by `scheduler`.
    """
    refreshed: list[str] = []

    @contextmanager
    def runtime(**_kwargs: object) -> Iterator[object]:
        yield SimpleNamespace(
            unit_of_work_factory=unit_of_work_factory,
            settings=SimpleNamespace(sources=TEST_SOURCES),
        )

    monkeypatch.setattr(tasks, "worker_runtime", runtime)
    monkeypatch.setattr(tasks.run_refresh, "delay", refreshed.append)

    tasks.schedule_refresh.run("entity")

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
        ("entity_refresh", "queued", "scheduler", "mgi"),
        ("entity_refresh", "queued", "scheduler", "rgd"),
    ]
    assert sorted(refreshed) == job_ids


def test_refresh_all_skips_a_group_with_an_unfinished_cutover(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Refresh-all leaves out a group whose cutover is unfinished."""
    service = _service(unit_of_work_factory, [])
    service.start_cutover(context=system_context("admin"), source_key="mgi-gpad")

    started = service.start(
        RefreshKindName.ANNOTATION, context=system_context("scheduler"), source_key=None
    )

    assert _targets(started.jobs) == [(JobType.ANNOTATION_REFRESH, "rgd-gpad")]


def test_a_cutover_request_reuses_an_unfinished_cutover(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A second cutover request returns the first, still unfinished, cutover job."""
    service = _service(unit_of_work_factory, [])
    first = service.start_cutover(
        context=system_context("admin"), source_key="mgi-gpad"
    )
    again = service.start_cutover(
        context=system_context("admin"), source_key="mgi-gpad"
    )

    assert first.job_type is JobType.ANNOTATION_CUTOVER
    assert again.job_id == first.job_id
