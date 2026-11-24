"""Verify what a worker does with its messages when a deploy stops it."""

import signal
from pathlib import Path

import redis
from celery import Celery
from worker_processes import start_worker, stop, wait_for
from worker_shutdown_tasks import SLOW_TASK

EXIT_TIMEOUT = 15
"""Seconds the stopped worker may take to exit; far less than its slow task runs."""
QUEUE = "celery"
"""Redis list holding messages no worker has taken."""
UNACKED = "unacked"
"""Redis hash holding messages a worker has taken but not acknowledged."""


def test_a_stopped_worker_returns_its_running_and_reserved_messages(
    broker_url: str, tmp_path: Path
) -> None:
    """A worker stopped with SIGTERM puts its messages back on the queue and exits.

    The worker takes one message at a time, so a second task waits in the queue
    while the first runs. When the worker is stopped, it cancels the running task
    after its soft shutdown and returns that task's message too, so another worker
    can run both, instead of holding them until Redis redelivers them.
    """
    client = redis.Redis.from_url(broker_url)
    log = tmp_path / "worker.log"
    worker = start_worker(broker_url, log, include="worker_shutdown_tasks")
    try:
        producer = Celery(broker=broker_url)
        producer.send_task(SLOW_TASK, args=["first-started"])
        producer.send_task(SLOW_TASK, args=["second-started"])
        wait_for(
            lambda: client.exists("first-started"),
            f"the worker never started a task:\n{log.read_text()}",
        )
        assert client.llen(QUEUE) == 1

        worker.send_signal(signal.SIGTERM)
        assert worker.wait(EXIT_TIMEOUT) == 0, log.read_text()

        assert client.llen(QUEUE) == 2
        assert client.hlen(UNACKED) == 0
        assert not client.exists("second-started")
    finally:
        stop(worker)
