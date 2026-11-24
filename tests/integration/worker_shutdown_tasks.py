"""Define a slow task for the worker shutdown test.

Only the worker that `test_worker_shutdown.py` starts imports this module, through
Celery's `--include` option, so the task never exists in a deployed worker.
"""

import time

import redis

from standard_annotation_backend.config import get_settings
from standard_annotation_backend.workers.celery_app import celery_app

SLOW_TASK = "sabe.test.slow"
"""Name the test sends the slow task by."""


@celery_app.task(name=SLOW_TASK)
def slow(started_key: str) -> None:
    """Record that the task started, then run for longer than the test waits."""
    redis.Redis.from_url(get_settings().redis_url).set(started_key, "1")
    time.sleep(120)
