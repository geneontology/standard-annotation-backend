"""Create entity import and retirement jobs and send them to workers."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.entity_sources.registry import EntitySourceRegistry
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import (
    Job,
    JobService,
    job_from_record,
)

logger = logging.getLogger(__name__)

_DISPATCH_FAILURE = "Entity import could not be dispatched"


@dataclass(frozen=True, slots=True)
class _StartedJob:
    job: Job
    created: bool


class EntityImportRunService:
    """Start entity imports for configured sources and retire removed sources."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        registry: EntitySourceRegistry,
        dispatch: Callable[[Job], None],
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._registry = registry
        self._dispatch = dispatch

    def start(self, *, requested_by: str, source_key: str | None) -> tuple[Job, ...]:
        """Create or reuse jobs, then send each queued or running job to a worker.

        With `source_key`, one import job is started for that source. Without it,
        every configured source gets an import job, and every source that still
        has an active catalog but is no longer configured gets a retirement job.
        If a queued or running job of the same type already exists for a source,
        that job is returned instead of creating another.

        Jobs are sent to workers only after the transaction commits. Reused jobs
        are sent again because an earlier message may have been lost, for example
        when the message broker restarted. Sending a job twice is harmless: a
        worker that cannot take the job's execution lock returns immediately, and
        a worker that can take it continues the job. If sending fails, only a job
        created by this call is marked failed.

        Raises:
            UnknownEntitySourceError: If `source_key` is not configured.
        """
        if source_key is not None:
            self._registry.source(source_key)
        started: list[_StartedJob] = []
        with self._unit_of_work_factory() as uow:
            uow.entities.lock_job_starts()
            targets: list[tuple[JobType, str]]
            if source_key is not None:
                targets = [(JobType.ENTITY_IMPORT, source_key)]
            else:
                configured = set(self._registry.keys)
                targets = [
                    (JobType.ENTITY_CATALOG_RETIREMENT, key)
                    for key in uow.entities.active_source_keys()
                    if key not in configured
                ] + [(JobType.ENTITY_IMPORT, key) for key in self._registry.keys]
            for job_type, key in targets:
                record = uow.jobs.find_active(job_type=job_type, source_key=key)
                created = record is None
                if record is None:
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
            uow.commit()
        return tuple(self._dispatch_started(item) for item in started)

    def _dispatch_started(self, item: _StartedJob) -> Job:
        if item.job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            return item.job
        try:
            self._dispatch(item.job)
        except Exception as error:
            logger.error(
                "Entity job dispatch failed: job_id=%s failure_type=%s",
                item.job.job_id,
                type(error).__name__,
            )
            if item.created:
                return JobService(self._unit_of_work_factory).fail(
                    item.job.job_id, error=_DISPATCH_FAILURE
                )
        return item.job
