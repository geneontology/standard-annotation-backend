"""Create or reuse refresh jobs and send them to be run.

The API, the Celery Beat schedule, and the refresh CLI all start refreshes
through `RefreshStartService`, so every path reuses existing work the same way.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import (
    REFRESH_JOB_TYPES,
    RefreshKindName,
    refresh_dispatch_failure_message,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import (
    Job,
    JobService,
    job_from_record,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _StartedJob:
    """Pair a started job with whether this call created it."""

    job: Job
    created: bool


class RefreshStartService:
    """Start refreshes of one kind for one source or for all configured sources.

    Args:
        unit_of_work_factory: Opens the transaction that creates jobs.
        sources: Configured sources for every kind.
        dispatch: Sends one job to be run. Celery callers enqueue a task; the
            CLI runs the job in its own process.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        sources: RefreshSources,
        dispatch: Callable[[Job], None],
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._sources = sources
        self._dispatch = dispatch

    def start(
        self,
        kind: RefreshKindName,
        *,
        requested_by: str,
        source_key: str | None,
    ) -> tuple[Job, ...]:
        """Create or reuse jobs, commit them, then dispatch each unfinished job.

        With `source_key`, one refresh job is started for that source. Without
        it, every configured source of the kind gets a refresh job. For entity
        refreshes, every source that still has an active catalog but is no
        longer configured also gets a retirement job.

        If a queued or running job of the same type already exists for a source,
        that job is returned and dispatched again instead of creating another.
        If dispatching fails, only a job created by this call is marked failed.

        Returns:
            Retirement jobs first, then refresh jobs, each in source-key order.

        Raises:
            UnknownSourceError: If `source_key` is not configured for `kind`.
        """
        # Reject an unconfigured key before touching the database, so a typo
        # creates no job and no audit event.
        if source_key is not None:
            self._sources.source(kind, source_key)

        started: list[_StartedJob] = []
        with self._unit_of_work_factory() as uow:
            # Serialize job creation across every request, scheduler run, and
            # CLI invocation; see `acquire_refresh_start_lock`. The lock is held
            # until this transaction commits, so the "find, else create" below
            # cannot interleave with another start.
            uow.jobs.lock_refresh_starts()
            for job_type, key in self._targets(uow, kind, source_key):
                # Reuse a queued or running job for the same source instead of
                # creating a duplicate. A running job may have lost its worker,
                # so it is dispatched again below and resumes.
                record = uow.jobs.find_active(job_type=job_type, source_key=key)
                created = record is None
                if record is None:
                    # New jobs store only the source key. Workers look the
                    # source up in the sources file when they run, so a job
                    # never carries a URL that the file no longer lists.
                    record = uow.jobs.create(
                        job_type=job_type,
                        requested_by=requested_by,
                        parameters={"source_key": key},
                        now=datetime.now(UTC),
                    )
                    AuditService(uow.audit).record_job_lifecycle(
                        action=AuditAction.JOB_QUEUED, record=record
                    )
                started.append(_StartedJob(job_from_record(record), created))
            # Commit before dispatching: a worker that receives the message must
            # be able to read the job. Dispatching first could deliver a job ID
            # that does not exist yet.
            uow.commit()
        return tuple(self._dispatch_started(kind, item) for item in started)

    def _targets(
        self, uow: SqlAlchemyUnitOfWork, kind: RefreshKindName, source_key: str | None
    ) -> list[tuple[JobType, str]]:
        """List the job type and source key of every job to start."""
        refresh_type = REFRESH_JOB_TYPES[kind]
        if source_key is not None:
            return [(refresh_type, source_key)]
        configured = self._sources.keys(kind)
        retirements: list[tuple[JobType, str]] = []
        # Only entity catalogs are retired. A catalog whose source key was
        # removed from the sources file (or renamed) stays active until a
        # retirement job removes it. Reading active catalogs under the start
        # lock keeps this list consistent with the jobs created alongside it.
        if kind is RefreshKindName.ENTITY:
            retirements = [
                (JobType.ENTITY_RETIREMENT, key)
                for key in uow.entities.active_source_keys()
                if key not in configured
            ]
        return retirements + [(refresh_type, key) for key in configured]

    def _dispatch_started(self, kind: RefreshKindName, item: _StartedJob) -> Job:
        """Dispatch an unfinished job; fail it only if this call created it."""
        # `find_active` only returns queued or running jobs, and new jobs are
        # queued, so this guard is defensive.
        if item.job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            return item.job
        try:
            # Sending a job twice is harmless: a worker that cannot take the
            # job's execution lock returns at once, and one that can take it
            # continues the job from its stored state.
            self._dispatch(item.job)
        except Exception as error:
            # Log only the job ID and the exception type. Broker errors can
            # include connection URLs with credentials.
            logger.error(
                "Refresh job dispatch failed: job_id=%s failure_type=%s",
                item.job.job_id,
                type(error).__name__,
            )
            # A reused job may already be in the broker from an earlier
            # dispatch, so only a job created by this call is marked failed.
            if item.created:
                return JobService(self._unit_of_work_factory).fail(
                    item.job.job_id, error=refresh_dispatch_failure_message(kind)
                )
        return item.job
