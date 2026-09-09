"""Celery application shared by future asynchronous jobs."""

import logging

from celery import Celery
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
)
