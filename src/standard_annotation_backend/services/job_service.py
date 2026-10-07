"""Manage stored job states and their audit events."""

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
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import RefreshFailureCode
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.repositories import (
    InvalidJobTransitionError,
    JobNotFoundError,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.audit_service import AuditService

__all__ = ["InvalidJobTransitionError", "Job", "JobNotFoundError", "JobService"]

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
        job_type=JobType(record.job_type),
        status=JobStatus(record.status),
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
            mutation = unit_of_work.jobs.start(job_id, now=datetime.now(UTC))
            if mutation.changed:
                AuditService(unit_of_work.audit).record_job_lifecycle(
                    action=AuditAction.JOB_STARTED, record=mutation.record
                )
            result = job_from_record(mutation.record)
            unit_of_work.commit()
        return result

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

    def succeed(
        self,
        job_id: UUID,
        *,
        result: dict[str, object],
    ) -> Job:
        """Commit a running job's successful terminal result."""
        with self._unit_of_work_factory() as unit_of_work:
            mutation = unit_of_work.jobs.succeed(
                job_id,
                result=result,
                now=datetime.now(UTC),
            )
            if mutation.changed:
                AuditService(unit_of_work.audit).record_job_lifecycle(
                    action=AuditAction.JOB_SUCCEEDED, record=mutation.record
                )
            completed = job_from_record(mutation.record)
            unit_of_work.commit()
        return completed

    def fail_refresh(
        self,
        job_id: UUID,
        *,
        error: str,
        failure_code: RefreshFailureCode,
        failure_details: dict[str, object] | None = None,
        cleanup: Callable[[SqlAlchemyUnitOfWork], None] | None = None,
    ) -> Job:
        """Mark a refresh job failed and record why, in one transaction.

        The failed status, `{"phase": "failed", "failure_code": ...}` progress
        (plus `failure_details` when given), the failure audit event, and any
        `cleanup` commit together, so none is recorded without the others. The
        audit event keeps at most the first 10 entries of an `issues` list; the
        job keeps all of them.

        Args:
            job_id: Job to mark failed.
            error: Nonblank public error message.
            failure_code: Why the job failed.
            failure_details: JSON-compatible description of the failure.
            cleanup: Kind-specific work, such as deleting staging rows, done in
                the same transaction.

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
        with self._unit_of_work_factory() as unit_of_work:
            mutation = unit_of_work.jobs.fail(
                job_id, error=safe_error, now=datetime.now(UTC), progress=progress
            )
            if mutation.changed:
                AuditService(unit_of_work.audit).record_job_lifecycle(
                    action=AuditAction.JOB_FAILED,
                    record=mutation.record,
                    details=audit_details,
                )
            # Cleanup shares this transaction: if it raises, the failed status
            # and audit event roll back too, and a retry records all of them.
            if cleanup is not None:
                cleanup(unit_of_work)
            failed = job_from_record(mutation.record)
            unit_of_work.commit()
        return failed


def _audit_failure_details(details: dict[str, object]) -> dict[str, object]:
    """Keep the first 10 entries of an `issues` list for the audit event."""
    issues = details.get("issues")
    if not isinstance(issues, list):
        return details
    return {**details, "issues": issues[:MAX_AUDITED_FAILURE_ISSUES]}
