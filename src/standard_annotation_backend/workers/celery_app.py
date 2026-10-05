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


def beat_schedule(settings: Settings) -> dict[str, dict[str, object]]:
    """Return the Celery Beat schedule that refreshes each kind of reference data.

    Each kind gets one entry that runs `sab.refresh.schedule` with the kind's name,
    on the cron expression from that kind's `*_refresh_cron` setting.
    """
    crons = {
        "authorization": settings.authorization_refresh_cron,
        "ontology": settings.ontology_refresh_cron,
        "entity": settings.entity_refresh_cron,
        "annotation": settings.annotation_refresh_cron,
    }
    return {
        f"{kind}-refresh": {
            "task": "sab.refresh.schedule",
            "schedule": crontab.from_string(cron),
            "args": (kind,),
        }
        for kind, cron in crons.items()
    }


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
    beat_schedule=beat_schedule(settings),
)
