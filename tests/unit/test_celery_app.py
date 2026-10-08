"""Tests for Celery task delivery guarantees and the Beat schedule."""

import os
from unittest.mock import patch

from celery.schedules import crontab

from standard_annotation_backend.config import Settings
from standard_annotation_backend.workers.celery_app import beat_schedule, celery_app


def test_tasks_are_redelivered_after_worker_loss_and_retried_without_a_cap() -> None:
    """SAB tasks are redelivered after worker loss and retried indefinitely.

    Tasks acknowledge late and are rejected back to the broker when their worker
    dies, so unfinished work is delivered again. SAB tasks have no retry limit, so
    infrastructure errors are retried until they succeed.
    """
    celery_app.loader.import_default_modules()

    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    sab_tasks = [
        task
        for task in celery_app.tasks.values()
        if task.__module__ == "standard_annotation_backend.workers.tasks"
    ]
    assert sab_tasks
    assert all(task.max_retries is None for task in sab_tasks)


def test_beat_schedules_each_kind_on_its_own_cron_setting() -> None:
    """Beat starts each kind through `sab.refresh.schedule` on that kind's cron."""
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(
            _env_file=None,
            database_url="postgresql+psycopg://sab:sab@db:5432/sab",
            redis_url="redis://redis:6379/0",
            application_secret="test-secret",
            environment="testing",
            log_format="console",
            authorization_refresh_cron="1 0 * * *",
            ontology_refresh_cron="2 0 * * *",
            entity_refresh_cron="3 0 * * *",
            annotation_refresh_cron="4 0 * * *",
        )

    assert beat_schedule(settings) == {
        f"{kind}-refresh": {
            "task": "sab.refresh.schedule",
            "schedule": crontab(minute=minute, hour="0"),
            "args": (kind,),
        }
        for kind, minute in (
            ("authorization", "1"),
            ("ontology", "2"),
            ("entity", "3"),
            ("annotation", "4"),
        )
    }
