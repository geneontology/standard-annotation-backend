"""Verify creation, reuse, and dispatch of refresh jobs."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace

import pytest
from annotation_refresh_helpers import seed_group_import
from refresh_helpers import TEST_SOURCES, sources_with_entities
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from test_entity_refresh_service import publish_catalog

from standard_annotation_backend.domain.annotation_management import (
    AnnotationManagementMode,
)
from standard_annotation_backend.domain.auth import system_context
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import RefreshKindName
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.refresh_start_service import (
    RefreshStartService,
)
from standard_annotation_backend.workers import tasks


def _service(
    unit_of_work_factory: UnitOfWorkFactory, dispatched: list[Job]
) -> RefreshStartService:
    return RefreshStartService(unit_of_work_factory, TEST_SOURCES, dispatched.append)


def _publish_unconfigured_catalog(unit_of_work_factory: UnitOfWorkFactory) -> None:
    """Publish a `zfin` catalog, a source that `TEST_SOURCES` does not configure."""
    zfin = sources_with_entities(
        {"zfin": {"type": "https", "url": "https://example.org/zfin.gpi"}}
    )
    service = EntityRefreshService(unit_of_work_factory, zfin)
    publish_catalog(service, unit_of_work_factory, "ZFIN:1", source="zfin")


def _targets(jobs: tuple[Job, ...]) -> list[tuple[JobType, str]]:
    return [(job.job_type, str(job.parameters["source_key"])) for job in jobs]


def test_refresh_all_entities_retires_removed_sources_first(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Every configured source is refreshed and every removed one retired."""
    _publish_unconfigured_catalog(unit_of_work_factory)
    dispatched: list[Job] = []

    jobs = _service(unit_of_work_factory, dispatched).start(
        RefreshKindName.ENTITY, context=system_context("scheduler"), source_key=None
    )

    assert _targets(jobs) == [
        (JobType.ENTITY_RETIREMENT, "zfin"),
        (JobType.ENTITY_REFRESH, "mgi"),
        (JobType.ENTITY_REFRESH, "rgd"),
    ]
    assert all(job.status is JobStatus.QUEUED for job in jobs)
    assert all(job.requested_by == "scheduler" for job in jobs)
    assert [job.job_id for job in dispatched] == [job.job_id for job in jobs]
    assert all(
        job.parameters == {"source_key": key}
        for job, (_, key) in zip(jobs, _targets(jobs), strict=True)
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (RefreshKindName.AUTHORIZATION, [(JobType.AUTHORIZATION_REFRESH, "go-site")]),
        (RefreshKindName.ONTOLOGY, [(JobType.ONTOLOGY_REFRESH, "go")]),
    ],
)
def test_refresh_all_for_kinds_without_retirement(
    unit_of_work_factory: UnitOfWorkFactory,
    kind: RefreshKindName,
    expected: list[tuple[JobType, str]],
) -> None:
    """Authorization and ontology refreshes never create retirement jobs."""
    _publish_unconfigured_catalog(unit_of_work_factory)

    jobs = _service(unit_of_work_factory, []).start(
        kind, context=system_context("scheduler"), source_key=None
    )

    assert _targets(jobs) == expected


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
        RefreshKindName.ENTITY, context=system_context("admin"), source_key="mgi"
    )
    if starting_status is JobStatus.RUNNING:
        JobService(unit_of_work_factory).start(first[0].job_id)
    dispatched: list[Job] = []

    second = _service(unit_of_work_factory, dispatched).start(
        RefreshKindName.ENTITY, context=system_context("scheduler"), source_key="mgi"
    )

    assert [job.job_id for job in second] == [first[0].job_id]
    assert second[0].status is starting_status
    assert [job.job_id for job in dispatched] == [first[0].job_id]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 1


def test_dispatch_failure_fails_only_new_jobs_with_kind_message(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """If sending a new job fails, only that job fails, with a kind-specific error."""

    def dispatch(job: Job) -> None:
        if job.parameters["source_key"] == "mgi":
            raise ConnectionError("broker down")

    jobs = RefreshStartService(unit_of_work_factory, TEST_SOURCES, dispatch).start(
        RefreshKindName.ENTITY, context=system_context("scheduler"), source_key=None
    )

    by_key = {str(job.parameters["source_key"]): job for job in jobs}
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
    [first] = _service(unit_of_work_factory, []).start(
        RefreshKindName.ENTITY, context=system_context("admin"), source_key="mgi"
    )

    def dispatch(_job: Job) -> None:
        raise ConnectionError("broker down")

    [second] = RefreshStartService(unit_of_work_factory, TEST_SOURCES, dispatch).start(
        RefreshKindName.ENTITY, context=system_context("admin"), source_key="mgi"
    )

    assert second.job_id == first.job_id
    assert second.status is JobStatus.QUEUED


def test_concurrent_starts_on_independent_connections_create_one_job_each(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Two simultaneous starts on separate connections cannot duplicate jobs."""
    barrier = Barrier(2)

    def start(_: int) -> tuple[Job, ...]:
        barrier.wait()
        return _service(unit_of_work_factory, []).start(
            RefreshKindName.ENTITY, context=system_context("scheduler"), source_key=None
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(start, range(2))

    assert {job.job_id for job in first} == {job.job_id for job in second}
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
    retired: list[str] = []

    @contextmanager
    def runtime(**_kwargs: object) -> Iterator[object]:
        yield SimpleNamespace(
            unit_of_work_factory=unit_of_work_factory,
            settings=SimpleNamespace(sources=TEST_SOURCES),
        )

    monkeypatch.setattr(tasks, "worker_runtime", runtime)
    monkeypatch.setattr(tasks.run_refresh, "delay", refreshed.append)
    monkeypatch.setattr(tasks.run_entity_retirement, "delay", retired.append)

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
    assert retired == []


def test_refresh_all_annotations_skips_sab_managed_groups_and_never_cuts_over(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Refresh-all creates ordinary jobs only for groups GPAD still manages."""
    seed_group_import(
        session_factory,
        group_key="RGD",
        source_key="rgd-gpad",
        mode=AnnotationManagementMode.SAB_MANAGED,
    )
    dispatched: list[Job] = []

    jobs = _service(unit_of_work_factory, dispatched).start(
        RefreshKindName.ANNOTATION, context=system_context("scheduler"), source_key=None
    )

    assert _targets(jobs) == [(JobType.ANNOTATION_REFRESH, "mgi-gpad")]


def test_cutover_reuses_only_an_unfinished_cutover(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A cutover request reuses an unfinished cutover but never an ordinary refresh."""
    service = _service(unit_of_work_factory, [])
    (refresh,) = service.start(
        RefreshKindName.ANNOTATION,
        context=system_context("admin"),
        source_key="mgi-gpad",
    )

    cutover = service.start_cutover(
        context=system_context("admin"), source_key="mgi-gpad"
    )
    again = service.start_cutover(
        context=system_context("admin"), source_key="mgi-gpad"
    )

    assert cutover.job_type is JobType.ANNOTATION_CUTOVER
    assert cutover.job_id != refresh.job_id
    assert again.job_id == cutover.job_id
