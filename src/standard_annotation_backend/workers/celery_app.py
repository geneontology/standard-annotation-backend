"""Configure Celery workers and periodic tasks."""

import logging

from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging

from standard_annotation_backend.config import (
    get_logging_settings,
    get_settings,
)
from standard_annotation_backend.logging import configure_logging

settings = get_settings()


@setup_logging.connect
def configure_celery_logging(
    loglevel: int | str = logging.INFO,
    **_kwargs: object,
) -> None:
    """Keep Celery from replacing SAB's process-wide logging pipeline."""
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
    beat_schedule={
        "authorization-sync": {
            "task": "sab.authorization_sync.schedule",
            "schedule": crontab.from_string(settings.authorization_sync_cron),
        },
        "ontology-load": {
            "task": "sab.ontology_load.schedule",
            "schedule": crontab.from_string(settings.ontology_load_cron),
        },
    },
)
