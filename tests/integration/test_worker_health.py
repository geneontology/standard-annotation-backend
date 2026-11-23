"""Verify the health check of real worker and scheduler processes."""

import signal
import subprocess
from collections.abc import Callable
from pathlib import Path
from time import sleep

import pytest
import redis
from worker_processes import start_scheduler, start_worker, stop, wait_for

from standard_annotation_backend.workers import health

STARTERS: dict[health.Process, Callable[[str, Path], subprocess.Popen[bytes]]] = {
    health.Process.WORKER: start_worker,
    health.Process.SCHEDULER: start_scheduler,
}


@pytest.mark.parametrize("process", list(health.Process))
def test_a_running_process_is_healthy_until_its_main_loop_stops(
    process: health.Process, broker_url: str, tmp_path: Path
) -> None:
    """A started worker or scheduler passes its health check, and a frozen one fails.

    Freezing the process with SIGSTOP stands in for a main loop that has stopped:
    its heartbeat stops, and the check fails once the heartbeat is older than the
    maximum age.
    """
    health.heartbeat_path(process).unlink(missing_ok=True)
    log = tmp_path / f"{process.value}.log"
    started = STARTERS[process](broker_url, log)
    try:
        wait_for(
            lambda: health.main([process.value]) == 0,
            f"the {process.value} never became healthy:\n{log.read_text()}",
        )

        started.send_signal(signal.SIGSTOP)
        sleep(3)
        assert health.main([process.value, "--max-age", "2"]) == 1
    finally:
        started.send_signal(signal.SIGCONT)
        stop(started)


def test_a_worker_beats_again_after_reconnecting_to_redis(
    broker_url: str, tmp_path: Path
) -> None:
    """A worker whose Redis connection drops resumes its heartbeat once reconnected.

    Celery restarts the worker's consumer after a lost connection; the heartbeat
    must restart with it, or a worker that recovered from a Redis restart would be
    replaced for failing its health check.
    """
    path = health.heartbeat_path(health.Process.WORKER)
    path.unlink(missing_ok=True)
    log = tmp_path / "worker.log"
    worker = start_worker(broker_url, log)
    try:
        # The worker connects its consumer after its first heartbeat, and logs
        # "ready." once connected.
        wait_for(
            lambda: "ready." in log.read_text(),
            f"the worker never connected:\n{log.read_text()}",
        )
        client = redis.Redis.from_url(broker_url)
        own = client.client_id()
        dropped = [
            entry["id"]
            for entry in client.client_list()
            if entry["db"] == "15" and int(entry["id"]) != own
        ]
        assert dropped, "the worker has no Redis connection to drop"
        for client_id in dropped:
            client.client_kill_filter(_id=client_id)
        wait_for(
            lambda: "Connection to broker lost" in log.read_text(),
            f"the worker never noticed the lost connection:\n{log.read_text()}",
        )
        path.unlink(missing_ok=True)

        wait_for(
            path.exists,
            f"the worker never beat again after reconnecting:\n{log.read_text()}",
        )
    finally:
        stop(worker)
