"""Verify the lifecycle every annotation scan job shares.

Tests that depend on what each scan does run for both term replacement and QC
report jobs; tests of the shared runner alone use one of them. Redelivery of a
running job is covered by `test_qc_report_runner.py`.
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from refresh_helpers import build_scan_runner
from seeding import create_job, deactivate_reference_data
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_entity_refresh_concurrency import TIMEOUT, wait_for_lock

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    scan_lock_value,
    try_advisory_lock,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_scan import (
    AnnotationScanBusyError,
    queue_or_join,
)
from standard_annotation_backend.services.job_service import Job, JobService

FAILURE_MESSAGES = {
    JobType.TERM_REPLACEMENT: "Term replacement failed",
    JobType.ANNOTATION_QC_REPORT: "QC report failed",
}
scan_job_types = pytest.mark.parametrize("job_type", list(FAILURE_MESSAGES))


@pytest.fixture(autouse=True)
def _reference(active_annotation_subjects: None) -> None:
    """Load the reference data that every scan requires."""


def _queue(
    unit_of_work_factory: UnitOfWorkFactory,
    job_type: JobType,
    group_key: str | None = None,
) -> Job:
    with unit_of_work_factory() as uow:
        job = queue_or_join(
            uow, job_type=job_type, group_key=group_key, requested_by="curator"
        )
        uow.commit()
    return job


def _left_running(
    unit_of_work_factory: UnitOfWorkFactory,
    job_type: JobType,
    group_key: str | None = None,
) -> Job:
    """Return a started job whose worker stopped and whose message was lost."""
    job = _queue(unit_of_work_factory, job_type, group_key)
    return JobService(unit_of_work_factory).start(job.job_id)


def _run(
    unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine, job: Job
) -> Job:
    build_scan_runner(database_engine, unit_of_work_factory).run(job.job_id)
    return JobService(unit_of_work_factory).find(job.job_id)


def test_a_job_waits_while_another_job_of_its_type_and_group_runs(
    unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine
) -> None:
    """While another job of its type and group runs, the job stays queued.

    The other job holds the `ANNOTATION_SCAN` lock on its own connection, and
    the run raises `AnnotationScanBusyError` so that its task retries later.
    """
    job = _queue(unit_of_work_factory, JobType.TERM_REPLACEMENT, "MGI")

    with try_advisory_lock(
        database_engine,
        LockNamespace.ANNOTATION_SCAN,
        scan_lock_value(JobType.TERM_REPLACEMENT, "MGI"),
    ) as held:
        assert held
        with pytest.raises(AnnotationScanBusyError):
            build_scan_runner(database_engine, unit_of_work_factory).run(job.job_id)

    assert JobService(unit_of_work_factory).find(job.job_id).status is (
        JobStatus.QUEUED
    )


def test_jobs_of_another_type_or_group_run_alongside_a_running_job(
    unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine
) -> None:
    """A running QC report for MGI blocks neither MGI's term replacement nor others.

    A QC report for all groups is not blocked by one for a single group either.
    """
    replacement = _queue(unit_of_work_factory, JobType.TERM_REPLACEMENT, "MGI")
    all_groups = _queue(unit_of_work_factory, JobType.ANNOTATION_QC_REPORT)

    with try_advisory_lock(
        database_engine,
        LockNamespace.ANNOTATION_SCAN,
        scan_lock_value(JobType.ANNOTATION_QC_REPORT, "MGI"),
    ) as held:
        assert held
        finished = [
            _run(unit_of_work_factory, database_engine, job)
            for job in (replacement, all_groups)
        ]

    assert [job.status for job in finished] == [JobStatus.SUCCEEDED] * 2


def test_a_finished_job_is_not_run_again(
    unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine
) -> None:
    """Running a job that already succeeded, as after a redelivery, changes nothing."""
    finished = _run(
        unit_of_work_factory,
        database_engine,
        _queue(unit_of_work_factory, JobType.ANNOTATION_QC_REPORT),
    )
    assert finished.status is JobStatus.SUCCEEDED

    again = _run(unit_of_work_factory, database_engine, finished)

    assert again == finished


@scan_job_types
def test_a_job_with_invalid_parameters_fails_without_running(
    job_type: JobType, unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine
) -> None:
    """Parameters other than `{}` or `{"group_key": ...}` fail the job at once."""
    job = create_job(
        unit_of_work_factory, job_type=job_type, parameters={"source_key": "mgi"}
    )

    failed = _run(unit_of_work_factory, database_engine, job)

    assert failed.status is JobStatus.FAILED
    assert failed.error == FAILURE_MESSAGES[job_type]
    assert failed.progress == {"phase": "failed", "failure_code": "invalid_parameters"}


@scan_job_types
def test_a_job_with_nothing_to_scan_still_requires_reference_data(
    job_type: JobType,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A group with no annotations fails with `reference_data_unavailable` too."""
    deactivate_reference_data(session_factory, SnapshotKind.ONTOLOGY, "go")

    failed = _run(
        unit_of_work_factory,
        database_engine,
        _queue(unit_of_work_factory, job_type, "ZFIN"),
    )

    assert failed.status is JobStatus.FAILED
    assert failed.error == FAILURE_MESSAGES[job_type]
    assert failed.progress["failure_code"] == "reference_data_unavailable"


@scan_job_types
def test_a_job_left_running_is_abandoned_when_the_next_job_of_its_groups_starts(
    job_type: JobType, unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine
) -> None:
    """An older running job that no process runs fails as `abandoned`.

    The next job of the same type and owning group fails it when it starts.
    """
    stuck = _left_running(unit_of_work_factory, job_type)
    job = _queue(unit_of_work_factory, job_type)

    finished = _run(unit_of_work_factory, database_engine, job)

    abandoned = JobService(unit_of_work_factory).find(stuck.job_id)
    assert finished.status is JobStatus.SUCCEEDED
    assert abandoned.status is JobStatus.FAILED
    assert abandoned.error == FAILURE_MESSAGES[job_type]
    assert abandoned.progress == {"phase": "failed", "failure_code": "abandoned"}


def test_a_job_that_finishes_while_the_next_job_starts_keeps_its_success(
    unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine
) -> None:
    """An older job whose success commits while the next job starts is not abandoned.

    The next job waits for the older job's transaction, sees that it succeeded,
    and runs.
    """
    jobs = JobService(unit_of_work_factory)
    stuck = _left_running(unit_of_work_factory, JobType.ANNOTATION_QC_REPORT)
    job = _queue(unit_of_work_factory, JobType.ANNOTATION_QC_REPORT)

    with unit_of_work_factory() as uow:
        jobs.record_success(
            uow, stuck.job_id, result={"done": True}, progress={"phase": "completed"}
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(_run, unit_of_work_factory, database_engine, job)
            wait_for_lock(database_engine)
            uow.commit()
            finished = running.result(TIMEOUT)

    assert finished.status is JobStatus.SUCCEEDED
    assert jobs.find(stuck.job_id).status is JobStatus.SUCCEEDED


def test_only_running_jobs_of_the_same_type_and_groups_are_abandoned(
    unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine
) -> None:
    """Jobs of another type, or covering other groups, keep running."""
    other_type = _left_running(unit_of_work_factory, JobType.TERM_REPLACEMENT)
    other_groups = _left_running(
        unit_of_work_factory, JobType.ANNOTATION_QC_REPORT, "MGI"
    )

    _run(
        unit_of_work_factory,
        database_engine,
        _queue(unit_of_work_factory, JobType.ANNOTATION_QC_REPORT),
    )

    jobs = JobService(unit_of_work_factory)
    assert jobs.find(other_type.job_id).status is JobStatus.RUNNING
    assert jobs.find(other_groups.job_id).status is JobStatus.RUNNING
