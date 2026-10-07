"""Verify job state changes and their audit events."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from seeding import create_job
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import RefreshFailureCode
from standard_annotation_backend.persistence.models import AuditEventRecord, JobRecord
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.job_service import (
    InvalidJobTransitionError,
    JobService,
)


def _service(factory: UnitOfWorkFactory) -> JobService:
    return JobService(factory)


def _audit_actions(session_factory: sessionmaker[Session]) -> list[str]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(AuditEventRecord.action).order_by(AuditEventRecord.created_at)
            )
        )


def test_job_lifecycle_commits_state_and_audit_together(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Queue, start, and success transitions retain matching audit history."""
    service = _service(unit_of_work_factory)
    created = create_job(
        unit_of_work_factory,
        job_type=JobType.AUTHORIZATION_REFRESH,
        requested_by="scheduler",
        parameters={
            "source_repository": "geneontology/go-site",
            "source_ref": "master",
        },
    )
    assert created.status is JobStatus.QUEUED
    assert created.progress == {}
    assert created.warnings == ()
    assert created.result is None

    running = service.start(created.job_id)
    assert running.status is JobStatus.RUNNING
    assert running.started_at is not None

    completed = service.succeed(
        created.job_id,
        result={"sync_id": "00000000-0000-0000-0000-000000000027"},
    )
    assert completed.status is JobStatus.SUCCEEDED
    assert completed.result == {"sync_id": "00000000-0000-0000-0000-000000000027"}
    assert completed.completed_at is not None
    assert _audit_actions(session_factory) == [
        "job.queued",
        "job.started",
        "job.succeeded",
    ]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 1
        assert {
            event.job_id for event in session.scalars(select(AuditEventRecord)).all()
        } == {created.job_id}


def test_progress_replaces_counts_and_warnings_for_a_running_job(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Progress updates replace the public snapshot without changing state."""
    service = _service(unit_of_work_factory)
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.AUTHORIZATION_REFRESH,
        requested_by="curator",
        parameters={},
    )
    service.start(job.job_id)
    updated = service.update_progress(
        job.job_id,
        progress={"processed": 8, "accepted": 7},
        warnings=("One row was skipped",),
    )
    assert updated.status is JobStatus.RUNNING
    assert updated.progress == {"processed": 8, "accepted": 7}
    assert updated.warnings == ("One row was skipped",)


@pytest.mark.parametrize("start_first", [False, True])
def test_job_can_fail_before_or_after_worker_start(
    unit_of_work_factory: UnitOfWorkFactory,
    start_first: bool,
) -> None:
    """Dispatch and execution failures both become valid terminal job records."""
    service = _service(unit_of_work_factory)
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.AUTHORIZATION_REFRESH,
        requested_by="scheduler",
        parameters={},
    )
    if start_first:
        service.start(job.job_id)
    failed = service.fail_refresh(
        job.job_id,
        error="Authorization synchronization failed",
        failure_code=RefreshFailureCode.SOURCE_ERROR,
    )
    assert failed.status is JobStatus.FAILED
    assert failed.started_at is not None if start_first else failed.started_at is None
    assert failed.completed_at is not None
    assert failed.error == "Authorization synchronization failed"


def test_blank_failure_is_rejected_before_state_changes(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A job cannot persist a blank or whitespace-only public failure summary."""
    service = _service(unit_of_work_factory)
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.AUTHORIZATION_REFRESH,
        requested_by="scheduler",
        parameters={},
    )
    with pytest.raises(ValueError, match="nonblank"):
        service.fail_refresh(
            job.job_id, error="  ", failure_code=RefreshFailureCode.SOURCE_ERROR
        )
    assert service.start(job.job_id).status is JobStatus.RUNNING


def test_repeated_transitions_are_idempotent_but_terminal_state_cannot_change(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Repeated transitions add no audit event and cannot change a final result."""
    service = _service(unit_of_work_factory)
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.AUTHORIZATION_REFRESH,
        requested_by="scheduler",
        parameters={},
    )
    first_running = service.start(job.job_id)
    repeated_running = service.start(job.job_id)
    assert repeated_running == first_running

    result: dict[str, object] = {"sync_id": str(UUID(int=27))}
    first_success = service.succeed(job.job_id, result=result)
    repeated_success = service.succeed(job.job_id, result=result)
    assert repeated_success == first_success
    assert service.start(job.job_id) == first_success
    assert _audit_actions(session_factory) == [
        "job.queued",
        "job.started",
        "job.succeeded",
    ]

    with pytest.raises(InvalidJobTransitionError):
        service.fail_refresh(
            job.job_id, error="Too late", failure_code=RefreshFailureCode.SOURCE_ERROR
        )


def test_conflicting_repeated_success_is_rejected(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A terminal success cannot be replayed with different public output."""
    service = _service(unit_of_work_factory)
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.AUTHORIZATION_REFRESH,
        requested_by="scheduler",
        parameters={},
    )
    service.start(job.job_id)
    service.succeed(job.job_id, result={"value": 1})
    with pytest.raises(InvalidJobTransitionError):
        service.succeed(job.job_id, result={"value": 2})


def test_concurrent_start_records_one_transition(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Concurrent start requests change and audit the job exactly once."""
    service = _service(unit_of_work_factory)
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.AUTHORIZATION_REFRESH,
        requested_by="scheduler",
        parameters={},
    )
    barrier = Barrier(2)

    def start() -> JobStatus:
        barrier.wait()
        return service.start(job.job_id).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: start(), range(2)))

    assert results == (JobStatus.RUNNING, JobStatus.RUNNING)
    assert _audit_actions(session_factory).count("job.started") == 1


def _running_refresh(factory: UnitOfWorkFactory) -> UUID:
    service = _service(factory)
    job = create_job(
        factory,
        job_type=JobType.ENTITY_REFRESH,
        requested_by="scheduler",
        parameters={"source_key": "mgi"},
    )
    service.start(job.job_id)
    return job.job_id


def _failure_audits(session_factory: sessionmaker[Session]) -> list[AuditEventRecord]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.action == AuditAction.JOB_FAILED
                )
            )
        )


def test_failed_refresh_records_code_and_details_with_truncated_audit(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """The job keeps every reported issue; its failure audit event keeps the first 10."""
    service = _service(unit_of_work_factory)
    job_id = _running_refresh(unit_of_work_factory)
    issues = [{"line_number": line} for line in range(12)]

    failed = service.fail_refresh(
        job_id,
        error="Entity refresh failed",
        failure_code=RefreshFailureCode.ROW_VALIDATION,
        failure_details={"issue_count": 12, "issues": issues},
    )

    assert failed.status is JobStatus.FAILED
    assert failed.error == "Entity refresh failed"
    assert failed.progress == {
        "phase": "failed",
        "failure_code": "row_validation",
        "failure_details": {"issue_count": 12, "issues": issues},
    }
    [audit] = _failure_audits(session_factory)
    assert audit.details["failure_code"] == "row_validation"
    assert audit.details["failure_details"] == {
        "issue_count": 12,
        "issues": issues[:10],
    }


def test_failed_refresh_without_details_records_only_the_code(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A failure without details stores just the phase and failure code."""
    service = _service(unit_of_work_factory)
    job_id = _running_refresh(unit_of_work_factory)

    failed = service.fail_refresh(
        job_id, error="Entity refresh failed", failure_code=RefreshFailureCode.TIMEOUT
    )

    assert failed.progress == {"phase": "failed", "failure_code": "timeout"}
    [audit] = _failure_audits(session_factory)
    assert "failure_details" not in audit.details


def test_failed_refresh_cleanup_commits_with_the_failure(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Cleanup runs in the failure's transaction, so a cleanup error records nothing."""
    service = _service(unit_of_work_factory)
    job_id = _running_refresh(unit_of_work_factory)
    cleaned: list[UUID] = []

    def broken_cleanup(_uow: SqlAlchemyUnitOfWork) -> None:
        raise RuntimeError("cleanup storage unavailable")

    with pytest.raises(RuntimeError):
        service.fail_refresh(
            job_id,
            error="Entity refresh failed",
            failure_code=RefreshFailureCode.SOURCE_ERROR,
            cleanup=broken_cleanup,
        )

    assert service.find(job_id).status is JobStatus.RUNNING
    assert _failure_audits(session_factory) == []

    service.fail_refresh(
        job_id,
        error="Entity refresh failed",
        failure_code=RefreshFailureCode.SOURCE_ERROR,
        cleanup=lambda _uow: cleaned.append(job_id),
    )

    assert service.find(job_id).status is JobStatus.FAILED
    assert cleaned == [job_id]
    assert len(_failure_audits(session_factory)) == 1
