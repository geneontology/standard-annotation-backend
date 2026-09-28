"""Create and run authorization synchronization jobs with Celery.

Celery Beat publishes `schedule_authorization_sync` because each scheduled
run needs a new job identifier from PostgreSQL. The scheduling task commits the
queued job before publishing `run_authorization_sync` with only that identifier.
The execution task loads its parameters and stores every state change in
PostgreSQL. Redis carries the Celery messages but does not store job state.

Using separate tasks records dispatch failures separately from execution
failures and allows other callers to execute an existing job. If Celery
redelivers an execution message, it uses the same job identifier.
"""

import logging
from uuid import UUID

from celery import Task
from celery.exceptions import Reject, Retry

from standard_annotation_backend.auth.authorization_source import (
    AuthorizationSourceClient,
)
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.locks import job_execution_lock
from standard_annotation_backend.workers.celery_app import celery_app
from standard_annotation_backend.workers.runtime import worker_runtime

logger = logging.getLogger(__name__)

_SYNC_FAILURE = "Authorization synchronization failed"
_DISPATCH_FAILURE = "Authorization synchronization could not be dispatched"
_TASK_UNAVAILABLE = "Authorization synchronization task could not persist state"


class WorkerTaskUnavailableError(RuntimeError):
    """Request a Celery retry without including infrastructure error details."""

    def __init__(self) -> None:
        super().__init__(_TASK_UNAVAILABLE)


def _source_client(parameters: dict[str, object]) -> AuthorizationSourceClient:
    """Build a source client from parameters stored on a job."""
    repository = parameters.get("source_repository")
    ref = parameters.get("source_ref")
    path = parameters.get("source_path")
    if (
        not isinstance(repository, str)
        or not isinstance(ref, str)
        or not isinstance(path, str)
    ):
        raise ValueError("invalid persisted authorization source")
    return AuthorizationSourceClient(repository=repository, ref=ref, path=path)


def _retry_safely(task: Task, job_id: str, failure_type: str) -> None:
    """Request a retry while logging only the job ID and exception type."""
    logger.error(
        "Authorization synchronization task unavailable: job_id=%s failure_type=%s",
        job_id,
        failure_type,
    )
    try:
        task.retry(
            exc=WorkerTaskUnavailableError(),
            countdown=30,
        )
    except Retry as retry:
        raise retry from None
    except WorkerTaskUnavailableError as error:
        raise error from None
    except Exception:
        raise Reject(WorkerTaskUnavailableError(), requeue=True) from None


@celery_app.task(bind=True, max_retries=None, name="sab.authorization_sync.run")
def run_authorization_sync(task: Task, job_id: str) -> None:
    """Run the authorization synchronization stored for one job.

    The Celery message contains only the job identifier. The task reads its
    parameters from PostgreSQL and writes progress and results there. If Celery
    redelivers the message, the job lock prevents overlapping executions and a
    completed job makes the later delivery do nothing.

    If the task cannot read or update job state, it asks Celery to retry. If the
    authorization synchronization fails, it records the configured public error
    and marks the job failed.
    """
    failure_type: str | None = None
    try:
        durable_job_id = UUID(job_id)
        with (
            worker_runtime() as runtime,
            job_execution_lock(runtime.engine, durable_job_id) as acquired,
        ):
            if not acquired:
                return
            job = runtime.jobs.start(durable_job_id)
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                return
            try:
                if job.job_type is not JobType.AUTHORIZATION_SYNC:
                    raise ValueError("unexpected job type")
                source_client = _source_client(job.parameters)
                persisted_sha = job.parameters.get("source_commit_sha")
                if persisted_sha is None:
                    source = source_client.fetch()
                    job = runtime.jobs.update_parameters(
                        job.job_id,
                        parameters={
                            **job.parameters,
                            "source_commit_sha": source.source_commit_sha,
                        },
                    )
                elif isinstance(persisted_sha, str):
                    source = source_client.fetch_at_commit(persisted_sha)
                else:
                    raise ValueError("invalid persisted source commit")
                sync = runtime.authorization_sync.synchronize(
                    source.yaml_text,
                    source.source_repository,
                    source.source_commit_sha,
                    actor_id=job.requested_by,
                    job_id=job.job_id,
                )
                runtime.jobs.succeed(
                    job.job_id,
                    result={
                        "sync_id": str(sync.sync_id),
                        "source_repository": sync.source_repository,
                        "source_commit_sha": sync.source_commit_sha,
                        "user_count": sync.user_count,
                        "group_count": sync.group_count,
                        "assignment_count": sync.assignment_count,
                        "applied": sync.applied,
                    },
                )
            except Exception as error:
                logger.error(
                    "Authorization synchronization job failed: job_id=%s failure_type=%s",
                    durable_job_id,
                    type(error).__name__,
                )
                runtime.jobs.fail(durable_job_id, error=_SYNC_FAILURE)
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, job_id, failure_type)


@celery_app.task(bind=True, max_retries=None, name="sab.authorization_sync.schedule")
def schedule_authorization_sync(task: Task) -> None:
    """Create a scheduled job before publishing its execution task.

    Celery Beat invokes this task, which does not perform the synchronization.
    It commits a queued job with the configured source parameters and then
    publishes `run_authorization_sync` with the job identifier. If publication
    raises an exception, it marks the queued job failed.

    A Beat run does not have its own stored identifier. If the worker stops
    during job creation or publication, Celery redelivery may create another job
    for the same scheduled run.
    """
    failure_type: str | None = None
    try:
        with worker_runtime() as runtime:
            job = runtime.jobs.create(
                job_type=JobType.AUTHORIZATION_SYNC,
                requested_by="scheduler",
                parameters={
                    "source_repository": runtime.settings.authorization_source_repository,
                    "source_ref": runtime.settings.authorization_source_ref,
                    "source_path": runtime.settings.authorization_source_path,
                },
            )
            try:
                run_authorization_sync.delay(str(job.job_id))
            except Exception as error:
                logger.error(
                    "Authorization synchronization dispatch failed: job_id=%s failure_type=%s",
                    job.job_id,
                    type(error).__name__,
                )
                runtime.jobs.fail(job.job_id, error=_DISPATCH_FAILURE)
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, "scheduler", failure_type)
