"""Verify that triggers share one queued annotation scan job per type and group.

Both annotation scan job types are queued by `queue_or_join`, so term
replacement jobs stand for both here. QC report starts are covered through the
API in `test_qc_reports_api.py`.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from test_entity_refresh_concurrency import wait_for_lock

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.models import AuditEventRecord, JobRecord
from standard_annotation_backend.persistence.unit_of_work import (
    UnitOfWorkFactory,
    create_unit_of_work_factory,
)
from standard_annotation_backend.services.annotation_scan import (
    queue_or_join,
)
from standard_annotation_backend.services.job_service import Job, JobService

TIMEOUT = 10


def _queue(
    unit_of_work_factory: UnitOfWorkFactory,
    group_key: str | None,
    *,
    job_type: JobType = JobType.TERM_REPLACEMENT,
    actor: str = "cli",
) -> Job:
    with unit_of_work_factory() as uow:
        job = queue_or_join(
            uow, job_type=job_type, group_key=group_key, requested_by=actor
        )
        uow.commit()
    return job


def test_a_trigger_queues_a_job_with_the_requester_and_group(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """The queued job records its requester, its group, and a queued audit event."""
    job = _queue(unit_of_work_factory, "MGI", actor="curator")

    assert job.job_type is JobType.TERM_REPLACEMENT
    assert job.status is JobStatus.QUEUED
    assert job.requested_by == "curator"
    assert job.parameters == {"group_key": "MGI"}
    with session_factory() as session:
        actions = session.scalars(
            select(AuditEventRecord.action).where(AuditEventRecord.job_id == job.job_id)
        ).all()
    assert actions == [AuditAction.JOB_QUEUED]


def test_triggers_join_the_queued_job_of_their_type_and_group_only(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """The same type and group reuse the queued job; other groups or types do not."""
    first = _queue(unit_of_work_factory, None)
    again = _queue(unit_of_work_factory, None)
    group = _queue(unit_of_work_factory, "MGI")
    report = _queue(unit_of_work_factory, None, job_type=JobType.ANNOTATION_QC_REPORT)

    assert again.job_id == first.job_id
    assert group.job_id != first.job_id
    assert report.job_id != first.job_id


def test_a_running_job_is_not_joined(unit_of_work_factory: UnitOfWorkFactory) -> None:
    """Once a job starts, the next trigger for its type and group queues a new one."""
    first = _queue(unit_of_work_factory, None)
    JobService(unit_of_work_factory).start(first.job_id)

    second = _queue(unit_of_work_factory, None)

    assert second.job_id != first.job_id
    assert second.status is JobStatus.QUEUED


def test_concurrent_triggers_on_independent_connections_share_one_job(
    session_factory: sessionmaker[Session], database_engine: Engine
) -> None:
    """A second trigger waits for the first to commit, then joins its job."""
    queued = Event()
    release = Event()
    second_pid: list[int] = []
    second_ready = Event()

    def trigger(actor: str, *, first: bool) -> Job:
        with session_factory() as session:
            pid = session.scalar(text("SELECT pg_backend_pid()"))
            assert pid is not None
            if not first:
                second_pid.append(pid)
                second_ready.set()
                assert queued.wait(TIMEOUT)
            uow_factory = create_unit_of_work_factory(lambda: session)
            with uow_factory() as uow:
                job = queue_or_join(
                    uow,
                    job_type=JobType.TERM_REPLACEMENT,
                    group_key=None,
                    requested_by=actor,
                )
                if first:
                    queued.set()
                    assert release.wait(TIMEOUT)
                uow.commit()
        return job

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_job = pool.submit(trigger, "a", first=True)
        second_job = pool.submit(trigger, "b", first=False)
        assert queued.wait(TIMEOUT)
        assert second_ready.wait(TIMEOUT)
        # The second trigger waits on the start lock until the first commits.
        wait_for_lock(database_engine, second_pid[0])
        release.set()
        jobs = (first_job.result(TIMEOUT), second_job.result(TIMEOUT))

    assert jobs[0].job_id == jobs[1].job_id
    assert jobs[1].requested_by == "a"
    with session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(JobRecord)
            .where(JobRecord.job_type == JobType.TERM_REPLACEMENT)
        )
    assert count == 1


def test_a_job_cannot_start_until_a_trigger_that_joined_it_commits(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """Starting waits for the joining trigger, so the job reads what it changed.

    The trigger joins the queued job and holds its transaction open; a start on
    another connection waits until the trigger commits.
    """
    queued = _queue(unit_of_work_factory, None)
    joined = Event()
    release = Event()
    starter_ready = Event()
    starter_pid: list[int] = []

    def trigger() -> Job:
        with session_factory() as session:
            uow_factory = create_unit_of_work_factory(lambda: session)
            with uow_factory() as uow:
                job = queue_or_join(
                    uow,
                    job_type=JobType.TERM_REPLACEMENT,
                    group_key=None,
                    requested_by="b",
                )
                joined.set()
                assert release.wait(TIMEOUT)
                uow.commit()
        return job

    def start() -> None:
        assert joined.wait(TIMEOUT)
        with session_factory() as session:
            pid = session.scalar(text("SELECT pg_backend_pid()"))
            assert pid is not None
            starter_pid.append(pid)
            starter_ready.set()
            uow_factory = create_unit_of_work_factory(lambda: session)
            with uow_factory() as uow:
                uow.jobs.start(queued.job_id, now=datetime.now(UTC))
                uow.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        joining = pool.submit(trigger)
        starting = pool.submit(start)
        assert starter_ready.wait(TIMEOUT)
        try:
            wait_for_lock(database_engine, starter_pid[0])
        finally:
            release.set()
        joined_job = joining.result(TIMEOUT)
        starting.result(TIMEOUT)

    assert joined_job.job_id == queued.job_id
