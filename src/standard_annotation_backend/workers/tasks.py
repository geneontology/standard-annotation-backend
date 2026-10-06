"""Create and run reference data refresh jobs with Celery.

Four tasks make up the refresh pipeline:

- `sab.refresh.schedule(kind)` is published by Celery Beat once per refresh kind.
  It starts a job for every configured source of that kind through
  `RefreshStartService`, which commits each job to PostgreSQL before sending it.
  Annotation sources whose group is SAB-managed are skipped.
- `sab.refresh.run(job_id)` runs one authorization, ontology, entity, or
  annotation refresh job, or one annotation cutover job.
- `sab.entity_retirement.run(job_id)` retires the catalog of an entity source
  that is no longer configured.
- `sab.ontology.prune(ontology_key)` deletes old snapshot data. `RefreshRunner`
  schedules it after each ontology job finishes.

Scheduling and execution are separate tasks so that a job exists in PostgreSQL
before any worker is asked to run it. If publishing a message fails, the job is
already recorded and can be found, retried, or reported; if execution fails
midway, Celery redelivers only the execution task, and the schedule is not
repeated.

Messages carry only a job ID (or a kind or ontology key). Everything else, such
as parameters, progress, and results, is read from and written to PostgreSQL.
Redis carries the Celery messages but holds no job state, and a message never
contains data that could go stale or leak through the broker.
`RefreshRunner` makes redelivered messages safe to run again.

Every task retries on unexpected errors, logging only the exception type, so
infrastructure details such as connection strings never reach Celery's logs.
"""

import logging
from uuid import UUID

from celery import Task
from celery.exceptions import Reject, Retry

from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import RefreshKindName
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshBusyError,
)
from standard_annotation_backend.services.refresh_start_service import (
    RefreshStartService,
)
from standard_annotation_backend.workers.celery_app import celery_app
from standard_annotation_backend.workers.runtime import worker_runtime

logger = logging.getLogger(__name__)

_TASK_UNAVAILABLE = "Worker task could not persist state"


class WorkerTaskUnavailableError(RuntimeError):
    """Request a Celery retry without including infrastructure error details."""

    def __init__(self) -> None:
        super().__init__(_TASK_UNAVAILABLE)


@celery_app.task(bind=True, max_retries=None, name="sab.ontology.prune")
def prune_ontology_snapshots(task: Task, ontology_key: str) -> None:
    """Delete unneeded term and closure rows for one ontology.

    The task takes the same per-ontology lock as refreshes. When that lock is
    busy, or infrastructure is unavailable, it asks Celery to retry later.
    An unknown key has nothing to prune, so the task returns without retrying.
    """
    failure_type: str | None = None
    try:
        # `_enqueue_prune` is passed only so the runtime is built the same way
        # as for refresh tasks; pruning itself never enqueues another prune.
        with worker_runtime(enqueue_prune=_enqueue_prune) as runtime:
            # `prune` returns `False` only when another process holds the lock.
            # Raise so the retry below runs once that process has finished.
            if not runtime.ontology.prune(ontology_key):
                raise OntologyRefreshBusyError
    except Exception as error:
        failure_type = type(error).__name__
    # Retry outside the `except` block so the retry exception has no chained
    # cause that could carry infrastructure details into Celery's logs.
    if failure_type is not None:
        _retry_safely(task, f"ontology:{ontology_key}", failure_type)


def _retry_safely(task: Task, job_id: str, failure_type: str) -> None:
    """Request a retry while logging only the job ID and exception type."""
    logger.error(
        "Worker task unavailable: job_id=%s failure_type=%s",
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


@celery_app.task(bind=True, max_retries=None, name="sab.refresh.run")
def run_refresh(task: Task, job_id: str) -> None:
    """Run one refresh job of any kind.

    The message carries only the job ID; the job's parameters and every state
    change live in PostgreSQL. `RefreshRunner.run` handles locking, redelivery,
    recovery, and failures that retrying cannot fix. Any other error is logged by
    type only and retried after 30 seconds, keeping the job and its committed
    work.
    """
    failure_type: str | None = None
    try:
        # An invalid ID raises `ValueError` here and is retried like any other
        # unexpected error; SAB only ever sends IDs of committed jobs.
        durable_job_id = UUID(job_id)
        # The runtime is built per task so its database engine and HTTP client
        # are closed when the task ends.
        with worker_runtime(enqueue_prune=_enqueue_prune) as runtime:
            runtime.runner.run(durable_job_id)
    except Exception as error:
        failure_type = type(error).__name__
    # Retry outside the `except` block so the retry exception has no chained
    # cause that could carry infrastructure details into Celery's logs.
    if failure_type is not None:
        _retry_safely(task, job_id, failure_type)


@celery_app.task(bind=True, max_retries=None, name="sab.entity_retirement.run")
def run_entity_retirement(task: Task, job_id: str) -> None:
    """Retire the catalog of an entity source that is no longer configured.

    Retries follow the same rules as `run_refresh`.
    """
    failure_type: str | None = None
    try:
        durable_job_id = UUID(job_id)
        with worker_runtime(enqueue_prune=_enqueue_prune) as runtime:
            runtime.runner.run_retirement(durable_job_id)
    except Exception as error:
        failure_type = type(error).__name__
    # As in `run_refresh`, retry outside the `except` block.
    if failure_type is not None:
        _retry_safely(task, job_id, failure_type)


def dispatch_refresh_job(job: Job) -> None:
    """Send one job to the Celery task that runs its job type.

    Only the job ID is sent; the worker reads everything else from PostgreSQL.
    `RefreshStartService` calls this after committing the job.
    """
    task = (
        run_entity_retirement
        if job.job_type is JobType.ENTITY_RETIREMENT
        else run_refresh
    )
    task.delay(str(job.job_id))


@celery_app.task(bind=True, max_retries=None, name="sab.refresh.schedule")
def schedule_refresh(task: Task, kind: str) -> None:
    """Start a scheduled refresh of every configured source of one kind.

    Celery Beat runs this task once per kind on that kind's cron setting. If the
    task is retried after its jobs were committed, the start service reuses and
    re-sends those jobs instead of creating new ones.
    """
    try:
        refresh_kind = RefreshKindName(kind)
    except ValueError:
        # A misconfigured Beat entry cannot be fixed by retrying, so log it once
        # and stop instead of retrying forever.
        logger.error("Scheduled refresh ignored unknown kind: kind=%s", kind)
        return
    failure_type: str | None = None
    try:
        with worker_runtime(enqueue_prune=_enqueue_prune) as runtime:
            RefreshStartService(
                runtime.unit_of_work_factory,
                runtime.settings.sources,
                dispatch_refresh_job,
            ).start(refresh_kind, requested_by="scheduler", source_key=None)
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, "scheduler", failure_type)


def _enqueue_prune(ontology_key: str) -> None:
    """Send ontology pruning to its own Celery task after an ontology job ends."""
    prune_ontology_snapshots.delay(ontology_key)
