"""Verify that Celery stores job state in PostgreSQL."""

import os
from unittest.mock import patch

from celery.schedules import crontab

from standard_annotation_backend.config import Settings
from standard_annotation_backend.workers.celery_app import beat_schedule, celery_app


def test_celery_uses_json_late_acknowledgement_and_no_result_backend() -> None:
    """Tasks use JSON, late acknowledgement, and no Celery result backend."""
    assert celery_app.conf.task_ignore_result is True
    assert celery_app.conf.result_backend is None
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.result_serializer == "json"
    assert celery_app.conf.enable_utc is True
    assert celery_app.conf.timezone == "UTC"
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_celery_registers_exactly_the_refresh_tasks() -> None:
    """SAB's task module registers the four refresh tasks and no others."""
    celery_app.loader.import_default_modules()

    sab_tasks = {
        name
        for name, task in celery_app.tasks.items()
        if task.__module__ == "standard_annotation_backend.workers.tasks"
    }
    assert sab_tasks == {
        "sab.refresh.run",
        "sab.refresh.schedule",
        "sab.entity_retirement.run",
        "sab.ontology.prune",
    }
    for name in sab_tasks:
        assert celery_app.tasks[name].max_retries is None


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
