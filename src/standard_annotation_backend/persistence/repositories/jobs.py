"""Persist durable asynchronous jobs in caller-managed transactions."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.jobs import (
    ACTIVE_JOB_STATUSES,
    JobNotFoundError,
    JobStatus,
    JobType,
)
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    acquire_transaction_lock,
)
from standard_annotation_backend.persistence.models import JobRecord


def lock_job_record(session: Session, job_id: UUID) -> JobRecord | None:
    """Load a job under a row lock held until the transaction ends.

    The lock is PostgreSQL's `FOR NO KEY UPDATE`. It excludes other changes to
    the job, but not inserts of rows that refer to the job by foreign key, which
    lock the job only against changes to its key. A refresh job inserts such rows
    in its own transaction while other connections record its progress, and
    those progress updates must not wait for that transaction to end. The lock
    still conflicts with the `FOR UPDATE` lock that `JobRepository.find_queued`
    takes, so a job reused by a caller cannot start before that caller commits.

    The row is reloaded from the database even if the session already holds it,
    so callers see the state committed by any transaction that held the lock
    before them.

    Args:
        session: Session whose transaction holds the lock.
        job_id: Identifier of the job to lock.

    Returns:
        The locked job, or `None` if no job has the identifier.
    """
    return session.scalar(
        select(JobRecord)
        .where(JobRecord.job_id == job_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )


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
            job_type=job_type,
            status=JobStatus.QUEUED,
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
                JobRecord.job_type == job_type,
                JobRecord.status.in_(ACTIVE_JOB_STATUSES),
                JobRecord.parameters["source_key"].astext == source_key,
            )
            .order_by(JobRecord.created_at, JobRecord.job_id)
            .limit(1)
        )

    def lock_annotation_scan_starts(self) -> None:
        """Make annotation scan job creation run one caller at a time.

        The lock is held until the transaction ends, so two callers cannot both
        find no queued job and both create one. One lock covers both job types;
        creation is short, and a single key cannot deadlock with itself.
        """
        acquire_transaction_lock(self.session, LockNamespace.ANNOTATION_SCAN_START)

    def find_queued(
        self, *, job_type: JobType, parameters: dict[str, object]
    ) -> JobRecord | None:
        """Lock and return the oldest queued job with this type and these parameters.

        Callers use this to reuse a queued job instead of creating another. The
        parameters must match exactly. The row stays locked until the
        transaction ends, so the job cannot start until the caller that reused it
        commits. A job whose start has already committed is running, not queued,
        and is not returned.
        """
        return self.session.scalar(
            select(JobRecord)
            .where(
                JobRecord.job_type == job_type,
                JobRecord.status == JobStatus.QUEUED,
                JobRecord.parameters == cast(parameters, JSONB),
            )
            .order_by(JobRecord.created_at, JobRecord.job_id)
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def running(
        self,
        *,
        job_type: JobType,
        parameters: dict[str, object],
        excluding: UUID,
    ) -> list[JobRecord]:
        """Lock and return the running jobs with a type and parameters, oldest first.

        The rows take the same lock as `lock_job_record` and stay locked until
        the transaction ends. A job that another transaction is finishing is
        waited for, and PostgreSQL then checks its status again, so a job whose
        success or failure committed in the meantime is not returned.

        Args:
            job_type: The job type to match.
            parameters: The parameters to match exactly.
            excluding: A job to leave out, such as the caller's own job.
        """
        return list(
            self.session.scalars(
                select(JobRecord)
                .where(
                    JobRecord.job_type == job_type,
                    JobRecord.status == JobStatus.RUNNING,
                    JobRecord.parameters == cast(parameters, JSONB),
                    JobRecord.job_id != excluding,
                )
                .order_by(JobRecord.created_at, JobRecord.job_id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
        )

    def lock(self, job_id: UUID) -> JobRecord:
        """Load a job under a row lock held until the transaction ends.

        Raises:
            JobNotFoundError: If the job does not exist.
        """
        return self._lock(job_id)

    def get(self, job_id: UUID) -> JobRecord | None:
        """Return a job by identifier without applying request authorization."""
        return self.session.get(JobRecord, job_id)

    def start(self, job_id: UUID, *, now: datetime) -> JobMutation:
        """Move a queued job to running under a row lock."""
        record = self._lock(job_id)
        status = record.status
        if status is not JobStatus.QUEUED:
            return JobMutation(record, False)
        record.status = JobStatus.RUNNING
        record.started_at = now
        record.updated_at = now
        self.session.flush([record])
        return JobMutation(record, True)

    def record_sent(self, job_id: UUID, *, now: datetime) -> JobRecord:
        """Record under a row lock that a send of the job succeeded.

        Only the first successful send's time is kept. `updated_at` is left
        alone, because sending does not change the job's state.

        Raises:
            JobNotFoundError: If the job does not exist.
        """
        record = self._lock(job_id)
        if record.sent_at is None:
            record.sent_at = now
            self.session.flush([record])
        return record

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
        if record.status is not JobStatus.RUNNING:
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
        now: datetime,
        warnings: Sequence[str],
        progress: dict[str, object] | None = None,
    ) -> JobMutation:
        """Finish a running job successfully under a row lock.

        Args:
            job_id: The job to finish.
            result: The result to store.
            now: The completion time.
            warnings: The warnings to store; they replace any stored earlier.
            progress: Final progress to store with the success, if given.
        """
        record = self._lock(job_id)
        status = record.status
        if status is JobStatus.SUCCEEDED:
            if record.result == result:
                return JobMutation(record, False)
            raise InvalidJobTransitionError("job already succeeded with another result")
        if status is not JobStatus.RUNNING:
            raise InvalidJobTransitionError("only running jobs can succeed")
        record.status = JobStatus.SUCCEEDED
        record.result = result
        if progress is not None:
            record.progress = progress
        record.warnings = list(warnings)
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
        status = record.status
        if status is JobStatus.FAILED:
            if record.error == error:
                return JobMutation(record, False)
            raise InvalidJobTransitionError("job already failed with another error")
        if status not in ACTIVE_JOB_STATUSES:
            raise InvalidJobTransitionError("terminal job outcome cannot change")
        record.status = JobStatus.FAILED
        record.error = error
        if progress is not None:
            record.progress = progress
        record.completed_at = now
        record.updated_at = now
        self.session.flush([record])
        return JobMutation(record, True)

    def _lock(self, job_id: UUID) -> JobRecord:
        """Load a job under a row lock held until the transaction ends."""
        record = lock_job_record(self.session, job_id)
        if record is None:
            raise JobNotFoundError(job_id)
        return record
