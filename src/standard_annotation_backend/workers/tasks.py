"""Create and run jobs with Celery.

One task runs every job: `sabe.job.run(job_id)` runs a refresh, an annotation
cutover, a term replacement, or a QC report job, through `JobExecutors`. Two
tasks start jobs on Celery Beat's schedule:

- `sabe.refresh.schedule(kind)` starts a job for every configured source of that
  kind through `RefreshStartService`, which commits each job to PostgreSQL before
  sending it. Annotation sources whose group is SABE-managed, or has an
  unfinished cutover job, are skipped.
- `sabe.qc_report.schedule` starts the all-groups QC report.

Ontology activations and annotation publications queue term replacement jobs,
which are sent to `sabe.job.run` after the refresh commits.

Scheduling and execution are separate tasks so that a job exists in PostgreSQL
before any worker is asked to run it. If publishing a message fails, the job is
already recorded and can be found, retried, or reported; if execution fails
midway, Celery redelivers only the execution task, and the schedule is not
repeated.

Messages carry at most a job ID or a refresh kind. Everything else, such as
parameters, progress, and results, is read from and written to PostgreSQL. Redis
carries the Celery messages but holds no job state, and a message never contains
data that could become outdated or be exposed through the broker.
`RefreshRunner` and `AnnotationScanRunner` make redelivered messages safe to run
again.

Every task retries on unexpected errors, logging only the exception type, so
infrastructure details such as connection strings never reach Celery's logs.
"""

import logging
from uuid import UUID

from celery import Task
from celery.exceptions import Reject, Retry

from standard_annotation_backend.domain.auth import system_context
from standard_annotation_backend.domain.refresh import RefreshKindName
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.qc_report_service import QcReportService
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


@celery_app.task(bind=True, max_retries=None, name="sabe.job.run")
def run_job(task: Task, job_id: str) -> None:
    """Run one job of any type.

    The message carries only the job ID; the job's type, parameters, and every
    state change live in PostgreSQL. `JobExecutors` picks the runner for the
    job's type, which handles locking, redelivery, restarts, and failures that
    retrying cannot fix. Any other error is logged by type only and retried
    after 30 seconds. That includes `AnnotationScanBusyError`, raised when
    another job of the same type, covering the same groups, is running.
    """
    failure_type: str | None = None
    try:
        # An invalid ID raises `ValueError` here and is retried like any other
        # unexpected error; SABE only ever sends IDs of committed jobs.
        durable_job_id = UUID(job_id)
        # The runtime is built per task so its database engine and HTTP client
        # are closed when the task ends.
        with worker_runtime(dispatch_follow_up=dispatch_job) as runtime:
            runtime.executors.run(durable_job_id)
    except Exception as error:
        failure_type = type(error).__name__
    # Retry outside the `except` block so the retry exception has no chained
    # cause that could carry infrastructure details into Celery's logs.
    if failure_type is not None:
        _retry_safely(task, job_id, failure_type)


def dispatch_job(job: Job) -> None:
    """Send one committed job to `sabe.job.run`.

    Only the job ID is sent; the worker reads everything else from PostgreSQL.
    """
    run_job.delay(str(job.job_id))


@celery_app.task(bind=True, max_retries=None, name="sabe.refresh.schedule")
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
        with worker_runtime() as runtime:
            RefreshStartService(
                runtime.unit_of_work_factory,
                runtime.settings.sources,
                dispatch_job,
            ).start(refresh_kind, context=system_context("scheduler"), source_key=None)
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, "scheduler", failure_type)


@celery_app.task(bind=True, max_retries=None, name="sabe.qc_report.schedule")
def schedule_qc_report(task: Task) -> None:
    """Start the scheduled all-groups QC report.

    Celery Beat runs this task on `qc_report_cron`. If an all-groups report is
    already queued, that report is sent again instead of a new one being
    created. If the report cannot be sent and no earlier send of it succeeded,
    it fails with `dispatch_failed`.
    Unexpected errors are retried after 30 seconds.
    """
    failure_type: str | None = None
    try:
        with worker_runtime() as runtime:
            QcReportService(runtime.unit_of_work_factory, dispatch_job).start(
                system_context("scheduler"), None
            )
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, "scheduler", failure_type)
