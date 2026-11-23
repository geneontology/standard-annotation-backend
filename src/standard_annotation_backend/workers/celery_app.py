"""Configure Celery workers and periodic tasks."""

import logging

from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging

from standard_annotation_backend.config import (
    Settings,
    get_logging_settings,
    get_settings,
)
from standard_annotation_backend.logging import configure_logging

settings = get_settings()

VISIBILITY_TIMEOUT_SECONDS = 6 * 60 * 60
"""How long Redis waits before giving an unacknowledged message to another worker.

A message stays unacknowledged while its job runs (`task_acks_late`), and Redis
counts from when it delivered the message. A job that runs longer than this is
delivered again while still running; the duplicate returns at the job's execution
lock and acknowledges the message, leaving nothing to recover the job if its worker
then dies. So this must be longer than the longest job. It matters only after a
worker is killed without its soft shutdown; a stopped worker returns its messages.
"""


def beat_schedule(settings: Settings) -> dict[str, dict[str, object]]:
    """Return the Celery Beat schedule of refreshes and the all-groups QC report.

    Each kind of reference data gets one entry that runs `sabe.refresh.schedule`
    with the kind's name, on the cron expression from that kind's
    `*_refresh_cron` setting. The all-groups QC report runs
    `sabe.qc_report.schedule` on `qc_report_cron`.
    """
    crons = {
        "authorization": settings.authorization_refresh_cron,
        "rules": settings.rules_refresh_cron,
        "ontology": settings.ontology_refresh_cron,
        "entity": settings.entity_refresh_cron,
        "annotation": settings.annotation_refresh_cron,
    }
    schedule: dict[str, dict[str, object]] = {
        f"{kind}-refresh": {
            "task": "sabe.refresh.schedule",
            "schedule": crontab.from_string(cron),
            "args": (kind,),
        }
        for kind, cron in crons.items()
    }
    schedule["annotation-qc-report"] = {
        "task": "sabe.qc_report.schedule",
        "schedule": crontab.from_string(settings.qc_report_cron),
        "args": (),
    }
    return schedule


@setup_logging.connect
def configure_celery_logging(
    loglevel: int | str = logging.INFO,
    **_kwargs: object,
) -> None:
    """Keep Celery from replacing SABE's process-wide logging pipeline."""
    configure_logging(get_logging_settings().log_format, loglevel)


celery_app = Celery(
    "standard_annotation_backend",
    broker=settings.redis_url,
    include=["standard_annotation_backend.workers.tasks"],
)
celery_app.conf.update(
    task_ignore_result=True,
    result_backend=None,
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    enable_utc=True,
    timezone="UTC",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # A worker reserves no message beyond the ones it is running, so a stopped or
    # killed worker strands no job it never started.
    worker_prefetch_multiplier=1,
    worker_concurrency=settings.worker_concurrency,
    worker_soft_shutdown_timeout=settings.worker_soft_shutdown_seconds,
    broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT_SECONDS},
    beat_schedule=beat_schedule(settings),
)
