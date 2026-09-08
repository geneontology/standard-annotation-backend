"""Celery application shared by future asynchronous jobs."""

from celery import Celery

from standard_annotation_backend.config import get_settings

settings = get_settings()

celery_app = Celery(
    "standard_annotation_backend",
    broker=settings.redis_url,
)
