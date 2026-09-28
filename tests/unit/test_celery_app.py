"""Verify that Celery stores job state in PostgreSQL."""

from celery.schedules import crontab

from standard_annotation_backend.workers.celery_app import celery_app


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


def test_celery_discovers_execution_and_periodic_producer_tasks() -> None:
    """The worker and Beat register the execution and scheduling tasks."""
    celery_app.loader.import_default_modules()

    assert "sab.authorization_sync.run" in celery_app.tasks
    assert "sab.authorization_sync.schedule" in celery_app.tasks
    assert celery_app.tasks["sab.authorization_sync.run"].max_retries is None
    assert celery_app.tasks["sab.authorization_sync.schedule"].max_retries is None
    assert celery_app.conf.beat_schedule == {
        "authorization-sync": {
            "task": "sab.authorization_sync.schedule",
            "schedule": crontab(minute="0", hour="0"),
        }
    }
