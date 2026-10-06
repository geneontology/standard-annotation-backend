"""Persist durable asynchronous jobs in caller-managed transactions."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    acquire_transaction_lock,
)
from standard_annotation_backend.persistence.models import JobRecord


class JobNotFoundError(LookupError):
    """Report an operation that targets an unknown job."""


class InvalidJobTransitionError(RuntimeError):
    """Report a job that cannot enter the requested lifecycle state."""


@dataclass(frozen=True, slots=True)
class JobMutation:
    """Pair a job record with whether the current transaction changed it."""

    record: JobRecord
    changed: bool


class JobRepository:
    """Store and update jobs within caller-owned transactions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        job_type: JobType,
        requested_by: str,
        parameters: dict[str, object],
        now: datetime,
    ) -> JobRecord:
        """Store one queued job and flush its generated identifier."""
        record = JobRecord(
            job_type=job_type.value,
            status=JobStatus.QUEUED.value,
            requested_by=requested_by,
            parameters=parameters,
            progress={},
            warnings=[],
            created_at=now,
            updated_at=now,
        )
        self.session.add(record)
        self.session.flush([record])
        return record

    def lock_refresh_starts(self) -> None:
        """Make refresh job creation run one request at a time, for every kind.

        Starting a refresh first looks for a queued or running job for each
        source and creates one only when none exists. Without this lock, two
        requests running at once could both find no job and both create one.
        The lock is held until the transaction ends.

        One lock covers every kind. Starts are short and rare, so serializing
        them costs nothing noticeable, and a single key cannot deadlock with
        itself.
        """
        acquire_transaction_lock(self.session, LockNamespace.REFRESH_START)

    def find_active(self, *, job_type: JobType, source_key: str) -> JobRecord | None:
        """Return the oldest queued or running job of a type for one source."""
        return self.session.scalar(
            select(JobRecord)
            .where(
                JobRecord.job_type == job_type.value,
                JobRecord.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
                JobRecord.parameters["source_key"].astext == source_key,
            )
            .order_by(JobRecord.created_at, JobRecord.job_id)
            .limit(1)
        )

    def get(self, job_id: UUID) -> JobRecord | None:
        """Return a job by identifier without applying request authorization."""
        return self.session.get(JobRecord, job_id)

    def start(self, job_id: UUID, *, now: datetime) -> JobMutation:
        """Move a queued job to running under a row lock."""
        record = self._lock(job_id)
        status = JobStatus(record.status)
        if status is not JobStatus.QUEUED:
            return JobMutation(record, False)
        record.status = JobStatus.RUNNING.value
        record.started_at = now
        record.updated_at = now
        self.session.flush([record])
        return JobMutation(record, True)

    def update_progress(
        self,
        job_id: UUID,
        *,
        progress: dict[str, object],
        warnings: tuple[str, ...],
        now: datetime,
    ) -> JobMutation:
        """Replace progress for a running job under a row lock."""
        record = self._lock(job_id)
        if JobStatus(record.status) is not JobStatus.RUNNING:
            raise InvalidJobTransitionError("only running jobs can update progress")
        changed = record.progress != progress or tuple(record.warnings) != warnings
        if changed:
            record.progress = progress
            record.warnings = list(warnings)
            record.updated_at = now
            self.session.flush([record])
        return JobMutation(record, changed)

    def succeed(
        self,
        job_id: UUID,
        *,
        result: dict[str, object],
        artifact_uri: str | None,
        now: datetime,
    ) -> JobMutation:
        """Finish a running job successfully under a row lock."""
        record = self._lock(job_id)
        status = JobStatus(record.status)
        if status is JobStatus.SUCCEEDED:
            if record.result == result and record.artifact_uri == artifact_uri:
                return JobMutation(record, False)
            raise InvalidJobTransitionError("job already succeeded with another result")
        if status is not JobStatus.RUNNING:
            raise InvalidJobTransitionError("only running jobs can succeed")
        record.status = JobStatus.SUCCEEDED.value
        record.result = result
        record.artifact_uri = artifact_uri
        record.completed_at = now
        record.updated_at = now
        self.session.flush([record])
        return JobMutation(record, True)

    def fail(
        self,
        job_id: UUID,
        *,
        error: str,
        now: datetime,
        progress: dict[str, object] | None = None,
    ) -> JobMutation:
        """Finish a queued or running job with a public error message."""
        record = self._lock(job_id)
        status = JobStatus(record.status)
        if status is JobStatus.FAILED:
            if record.error == error:
                return JobMutation(record, False)
            raise InvalidJobTransitionError("job already failed with another error")
        if status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            raise InvalidJobTransitionError("terminal job outcome cannot change")
        record.status = JobStatus.FAILED.value
        record.error = error
        if progress is not None:
            record.progress = progress
        record.completed_at = now
        record.updated_at = now
        self.session.flush([record])
        return JobMutation(record, True)

    def _lock(self, job_id: UUID) -> JobRecord:
        """Load a job under a row lock held until the transaction ends."""
        record = self.session.scalar(
            select(JobRecord)
            .where(JobRecord.job_id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise JobNotFoundError(job_id)
        return record
