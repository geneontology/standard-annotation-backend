"""Start real worker and scheduler processes for tests, apart from other Redis users."""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep

from standard_annotation_backend.workers.worker import CELERY_APP

TIMEOUT = 30
"""Seconds to wait for a started process to do what a test expects."""


def _environment(broker_url: str) -> dict[str, str]:
    """Return an environment pointing a SABE process at `broker_url`."""
    return {
        **os.environ,
        "SABE_REDIS_URL": broker_url,
        "SABE_WORKER_SOFT_SHUTDOWN_SECONDS": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")]
        ),
    }


def _start(command: list[str], broker_url: str, log: Path) -> subprocess.Popen[bytes]:
    with log.open("wb") as output:
        return subprocess.Popen(
            command,
            env=_environment(broker_url),
            stdout=output,
            stderr=subprocess.STDOUT,
        )


def start_worker(
    broker_url: str, log: Path, *, include: str | None = None
) -> subprocess.Popen[bytes]:
    """Start a worker the way a deployment does, with a one-second soft shutdown.

    Args:
        broker_url: The Redis database the worker uses.
        log: Where the worker's output goes.
        include: A test-only task module for the worker to load.
    """
    command = [
        sys.executable,
        "-m",
        "standard_annotation_backend.workers.worker",
        "--loglevel=INFO",
    ]
    if include is not None:
        command += ["--include", include]
    return _start(command, broker_url, log)


def start_scheduler(broker_url: str, log: Path) -> subprocess.Popen[bytes]:
    """Start Celery Beat with SABE's schedule, keeping its schedule file beside `log`."""
    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        CELERY_APP,
        "beat",
        "--loglevel=INFO",
        "--schedule",
        str(log.with_name("beat-schedule")),
    ]
    return _start(command, broker_url, log)


def stop(process: subprocess.Popen[bytes]) -> None:
    """Stop `process` if it is still running, killing it if it does not exit."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def wait_for(condition_met: Callable[[], object], message: str) -> None:
    """Poll `condition_met` until it returns true, or fail with `message`."""
    deadline = monotonic() + TIMEOUT
    while monotonic() < deadline:
        if condition_met():
            return
        sleep(0.2)
    raise AssertionError(message)
