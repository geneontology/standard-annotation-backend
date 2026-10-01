"""Manage stored job states and their audit events."""

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
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.repositories import (
    InvalidJobTransitionError,
    JobNotFoundError,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.audit_service import AuditService

__all__ = ["InvalidJobTransitionError", "Job", "JobNotFoundError", "JobService"]


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
    artifact_uri: str | None
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
        artifact_uri=record.artifact_uri,
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

    def create(
        self,
        *,
        job_type: JobType,
        requested_by: str,
        parameters: dict[str, object],
    ) -> Job:
        """Create and audit a queued job."""
        now = datetime.now(UTC)
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.jobs.create(
                job_type=job_type,
                requested_by=requested_by,
                parameters=parameters,
                now=now,
            )
            AuditService(unit_of_work.audit).record_job_lifecycle(
                action=AuditAction.JOB_QUEUED, record=record
            )
            result = job_from_record(record)
            unit_of_work.commit()
        return result

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

    def update_parameters(
        self,
        job_id: UUID,
        *,
        parameters: dict[str, object],
    ) -> Job:
        """Replace parameters needed to resume a running job and commit them."""
        with self._unit_of_work_factory() as unit_of_work:
            mutation = unit_of_work.jobs.update_parameters(
                job_id,
                parameters=parameters,
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
        artifact_uri: str | None = None,
    ) -> Job:
        """Commit a running job's successful terminal result."""
        with self._unit_of_work_factory() as unit_of_work:
            mutation = unit_of_work.jobs.succeed(
                job_id,
                result=result,
                artifact_uri=artifact_uri,
                now=datetime.now(UTC),
            )
            if mutation.changed:
                AuditService(unit_of_work.audit).record_job_lifecycle(
                    action=AuditAction.JOB_SUCCEEDED, record=mutation.record
                )
            completed = job_from_record(mutation.record)
            unit_of_work.commit()
        return completed

    def fail(self, job_id: UUID, *, error: str) -> Job:
        """Commit a queued or running job's final public error."""
        safe_error = error.strip()
        if not safe_error:
            raise ValueError("job error must be nonblank")
        with self._unit_of_work_factory() as unit_of_work:
            mutation = unit_of_work.jobs.fail(
                job_id,
                error=safe_error,
                now=datetime.now(UTC),
            )
            if mutation.changed:
                AuditService(unit_of_work.audit).record_job_lifecycle(
                    action=AuditAction.JOB_FAILED, record=mutation.record
                )
            failed = job_from_record(mutation.record)
            unit_of_work.commit()
        return failed
