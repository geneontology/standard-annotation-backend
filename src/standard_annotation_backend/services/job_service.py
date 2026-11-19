"""Manage stored job states and their audit events."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.auth import (
    AuthorizationScope,
    PermissionAction,
    PermissionDeniedError,
    RequestContext,
    authorize_role,
)
from standard_annotation_backend.domain.jobs import (
    TERMINAL_JOB_STATUSES,
    JobFailureCode,
    JobNotFoundError,
    JobStatus,
    JobType,
    dispatch_failure_message,
)
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.repositories import (
    InvalidJobTransitionError,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.audit_service import AuditService

__all__ = ["InvalidJobTransitionError", "Job", "JobService"]

logger = logging.getLogger(__name__)

MAX_AUDITED_FAILURE_ISSUES = 10


@dataclass(frozen=True, slots=True)
class Job:
    """Represent job state returned by `JobService` operations."""

    job_id: UUID
    job_type: JobType
    status: JobStatus
    requested_by: str
    parameters: dict[str, object]
    progress: dict[str, object]
    warnings: tuple[str, ...]
    result: dict[str, object] | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


def job_from_record(record: JobRecord) -> Job:
    """Convert a stored job record to a service result."""
    return Job(
        job_id=record.job_id,
        job_type=record.job_type,
        status=record.status,
        requested_by=record.requested_by,
        parameters=dict(record.parameters),
        progress=dict(record.progress),
        warnings=tuple(record.warnings),
        result=None if record.result is None else dict(record.result),
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


class JobService:
    """Update job state and record matching audit events in one transaction."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def get(self, context: RequestContext, job_id: UUID) -> Job:
        """Return a job when the caller has the admin role and global scope."""
        authorize_role(context, PermissionAction.JOB_READ)
        if context.scope is not AuthorizationScope.GLOBAL:
            raise PermissionDeniedError

        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.jobs.get(job_id)
            if record is None:
                raise JobNotFoundError(job_id)
            return job_from_record(record)

    def find(self, job_id: UUID) -> Job:
        """Return a job for trusted internal callers such as workers and the CLI.

        Unlike `get`, this does not check a request's authorization.

        Raises:
            JobNotFoundError: If the job does not exist.
        """
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.jobs.get(job_id)
            if record is None:
                raise JobNotFoundError(job_id)
            return job_from_record(record)

    def start(self, job_id: UUID) -> Job:
        """Start a queued job or return its unchanged later state."""
        with self._unit_of_work_factory() as unit_of_work:
            result = self.record_start(unit_of_work, job_id)
            unit_of_work.commit()
        return result

    def record_start(self, unit_of_work: SqlAlchemyUnitOfWork, job_id: UUID) -> Job:
        """Start a queued job in the caller's transaction, or return its later state.

        The running status and the `job.started` audit event commit only when the
        caller commits. A job that is already running, or has finished, is
        returned unchanged.
        """
        mutation = unit_of_work.jobs.start(job_id, now=datetime.now(UTC))
        if mutation.changed:
            AuditService(unit_of_work.audit).record_job_lifecycle(
                action=AuditAction.JOB_STARTED, record=mutation.record
            )
        return job_from_record(mutation.record)

    def send(self, job: Job, *, dispatch: Callable[[Job], None]) -> Job:
        """Send a committed job to a worker, failing it if no send of it has succeeded.

        The job's row lock is held while `dispatch` runs and until the outcome
        is recorded, so sends of one job happen one at a time and each sees what
        the sends before it did. A successful send records `sent_at`. Sending a
        job twice is harmless: a worker that cannot take the job's execution
        lock returns at once. A job that has already finished is returned
        without being sent.

        If `dispatch` raises, only the job ID and exception type are logged,
        because broker errors can include connection URLs with credentials. The
        job is then failed with `dispatch_failed` if it is still queued and no
        send of it has succeeded, so the record says it will not run and the
        next trigger creates a new job. Otherwise it is left as it is: an
        earlier send put it in the broker, or a worker already started it. A
        broker can also raise after it accepted the message; a worker that later
        receives it finds the job failed and does nothing.

        `dispatch` must not wait for the job to start, or change its row on
        another connection, as running the job inline or Celery's eager mode
        would: that waits on the row lock this call holds, forever.

        Args:
            job: The committed job.
            dispatch: Sends the job to a worker.

        Returns:
            The job's state after sending.

        Raises:
            JobNotFoundError: If the job does not exist.
            Exception: Any error from recording the send or `dispatch_failed`.
        """
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.jobs.lock(job.job_id)
            if record.status in TERMINAL_JOB_STATUSES:
                return job_from_record(record)
            try:
                dispatch(job)
            except Exception as error:
                logger.error(
                    "Job dispatch failed: job_id=%s job_type=%s failure_type=%s",
                    job.job_id,
                    job.job_type.value,
                    type(error).__name__,
                )
                if record.status is not JobStatus.QUEUED or record.sent_at is not None:
                    return job_from_record(record)
                failed = self.record_failure(
                    unit_of_work,
                    job.job_id,
                    error=dispatch_failure_message(job.job_type),
                    failure_code=JobFailureCode.DISPATCH_FAILED,
                )
                unit_of_work.commit()
                return failed
            sent = job_from_record(
                unit_of_work.jobs.record_sent(job.job_id, now=datetime.now(UTC))
            )
            unit_of_work.commit()
        return sent

    def update_progress(
        self,
        job_id: UUID,
        *,
        progress: dict[str, object],
        warnings: tuple[str, ...] = (),
    ) -> Job:
        """Replace the latest progress snapshot for a running job."""
        with self._unit_of_work_factory() as unit_of_work:
            mutation = unit_of_work.jobs.update_progress(
                job_id,
                progress=progress,
                warnings=warnings,
                now=datetime.now(UTC),
            )
            result = job_from_record(mutation.record)
            unit_of_work.commit()
        return result

    def record_success(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        job_id: UUID,
        *,
        result: dict[str, object],
        progress: dict[str, object],
        warnings: tuple[str, ...] = (),
    ) -> Job:
        """Mark a running job succeeded in the caller's transaction.

        The success, final progress, warnings, and `job.succeeded` audit event
        commit only when the caller commits. A refresh job commits its success
        together with the data it stored, so an interrupted job never leaves
        stored data without a recorded success, or a success without its data.

        Args:
            unit_of_work: The caller's open transaction.
            job_id: The running job.
            result: The result to store.
            progress: The final progress to store.
            warnings: The warnings to store; they replace any stored earlier,
                so a job with none ends with none.

        Raises:
            InvalidJobTransitionError: If the job is not running, or already
                succeeded with another result.
        """
        mutation = unit_of_work.jobs.succeed(
            job_id,
            result=result,
            now=datetime.now(UTC),
            progress=progress,
            warnings=warnings,
        )
        if mutation.changed:
            AuditService(unit_of_work.audit).record_job_lifecycle(
                action=AuditAction.JOB_SUCCEEDED, record=mutation.record
            )
        return job_from_record(mutation.record)

    def fail(
        self,
        job_id: UUID,
        *,
        error: str,
        failure_code: JobFailureCode,
        failure_details: dict[str, object] | None = None,
        cleanup: Callable[[SqlAlchemyUnitOfWork], None] | None = None,
    ) -> Job:
        """Mark a job failed and record why, in one transaction.

        The failure and any `cleanup` commit together, so neither is recorded
        without the other. See `record_failure` for what is stored.

        Args:
            job_id: Job to mark failed.
            error: Nonblank public error message.
            failure_code: Why the job failed.
            failure_details: JSON-compatible description of the failure.
            cleanup: Job-specific work, such as deleting a report's stored rows,
                done in the same transaction.

        Raises:
            ValueError: If `error` is blank.
        """
        with self._unit_of_work_factory() as unit_of_work:
            failed = self.record_failure(
                unit_of_work,
                job_id,
                error=error,
                failure_code=failure_code,
                failure_details=failure_details,
            )
            # Cleanup shares this transaction: if it raises, the failed status
            # and audit event roll back too, and a retry records all of them.
            if cleanup is not None:
                cleanup(unit_of_work)
            unit_of_work.commit()
        return failed

    def record_failure(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        job_id: UUID,
        *,
        error: str,
        failure_code: JobFailureCode,
        failure_details: dict[str, object] | None = None,
    ) -> Job:
        """Mark a queued or running job failed in the caller's transaction.

        The failed status, `{"phase": "failed", "failure_code": ...}` progress
        (plus `failure_details` when given), and the `job.failed` audit event
        commit only when the caller commits. The audit event keeps at most the
        first 10 entries of an `issues` list; the job keeps all of them.

        Args:
            unit_of_work: The caller's open transaction.
            job_id: The job to fail.
            error: Nonblank public error message.
            failure_code: Why the job failed.
            failure_details: JSON-compatible description of the failure.

        Raises:
            ValueError: If `error` is blank.
        """
        safe_error = error.strip()
        if not safe_error:
            raise ValueError("job error must be nonblank")
        progress: dict[str, object] = {
            "phase": "failed",
            "failure_code": failure_code.value,
        }
        audit_details: dict[str, object] = {"failure_code": failure_code.value}
        if failure_details is not None:
            progress["failure_details"] = failure_details
            audit_details["failure_details"] = _audit_failure_details(failure_details)
        mutation = unit_of_work.jobs.fail(
            job_id, error=safe_error, now=datetime.now(UTC), progress=progress
        )
        if mutation.changed:
            AuditService(unit_of_work.audit).record_job_lifecycle(
                action=AuditAction.JOB_FAILED,
                record=mutation.record,
                details=audit_details,
            )
        return job_from_record(mutation.record)


def _audit_failure_details(details: dict[str, object]) -> dict[str, object]:
    """Keep the first 10 entries of an `issues` list for the audit event."""
    issues = details.get("issues")
    if not isinstance(issues, list):
        return details
    return {**details, "issues": issues[:MAX_AUDITED_FAILURE_ISSUES]}
