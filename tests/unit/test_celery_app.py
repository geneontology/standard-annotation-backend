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


def test_beat_schedules_one_refresh_per_kind() -> None:
    """Beat starts each kind through `sab.refresh.schedule` on its own cron."""
    assert celery_app.conf.beat_schedule == {
        "authorization-refresh": {
            "task": "sab.refresh.schedule",
            "schedule": crontab(minute="0", hour="0"),
            "args": ("authorization",),
        },
        "ontology-refresh": {
            "task": "sab.refresh.schedule",
            "schedule": crontab(minute="0", hour="2", day_of_week="1,3,5"),
            "args": ("ontology",),
        },
        "entity-refresh": {
            "task": "sab.refresh.schedule",
            "schedule": crontab(minute="0", hour="3"),
            "args": ("entity",),
        },
    }
