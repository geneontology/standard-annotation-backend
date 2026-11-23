"""Verify what a worker does with its messages when a deploy stops it."""

import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlsplit, urlunsplit

import pytest
import redis
from celery import Celery
from worker_shutdown_tasks import SLOW_TASK

from standard_annotation_backend.config import get_settings

TIMEOUT = 30
"""Seconds to wait for the worker to start a task."""
EXIT_TIMEOUT = 15
"""Seconds the stopped worker may take to exit; far less than its slow task runs."""
QUEUE = "celery"
"""Redis list holding messages no worker has taken."""
UNACKED = "unacked"
"""Redis hash holding messages a worker has taken but not acknowledged."""


@pytest.fixture
def broker_url() -> Iterator[str]:
    """Return an empty Redis database for this test, apart from other users of Redis."""
    url = urlunsplit(urlsplit(get_settings().redis_url)._replace(path="/15"))
    client = redis.Redis.from_url(url)
    client.flushdb()
    yield url
    client.flushdb()


def _start_worker(broker_url: str, log: Path) -> subprocess.Popen[bytes]:
    """Start a worker the way a deployment does, with a one-second soft shutdown."""
    env = {
        **os.environ,
        "SABE_REDIS_URL": broker_url,
        "SABE_WORKER_SOFT_SHUTDOWN_SECONDS": "1",
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")]
        ),
    }
    with log.open("wb") as output:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "standard_annotation_backend.workers.worker",
                "--loglevel=INFO",
                "--include",
                "worker_shutdown_tasks",
            ],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )


def _wait_for(condition_met: Callable[[], object], message: str) -> None:
    """Poll `condition_met` until it returns true, or fail with `message`."""
    deadline = monotonic() + TIMEOUT
    while monotonic() < deadline:
        if condition_met():
            return
        sleep(0.2)
    raise AssertionError(message)


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
    worker = _start_worker(broker_url, log)
    try:
        producer = Celery(broker=broker_url)
        producer.send_task(SLOW_TASK, args=["first-started"])
        producer.send_task(SLOW_TASK, args=["second-started"])
        _wait_for(
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
        if worker.poll() is None:
            worker.kill()
            worker.wait()
