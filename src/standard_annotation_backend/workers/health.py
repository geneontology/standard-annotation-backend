"""Check whether a worker or scheduler container's main process is still running.

A container platform such as ECS runs this as the container's health check:
`python -m standard_annotation_backend.workers.health worker` (or `scheduler`). It
succeeds when the process's heartbeat file was written recently. The worker's main
process writes its file every `HEARTBEAT_INTERVAL_SECONDS` from a timer, and Beat
writes its file on every scheduling pass, at least every `SCHEDULER_LOOP_SECONDS`
(`workers/heartbeat.py`). A main process
whose loop has stopped, for example one stuck reconnecting to Redis, stops writing,
and the check fails once the file is older than the maximum age.

The check judges the main process only. A worker's jobs run in a child process, so
a long job never fails it, and neither does a job stuck inside its own work.

The check runs often, so this module uses only the standard library and loads none
of SABE's application or settings.
"""

import argparse
import sys
import time
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

HEARTBEAT_DIRECTORY = Path("/tmp")
"""Where the heartbeat files are written; local to each container."""
HEARTBEAT_INTERVAL_SECONDS = 2
"""How often the worker writes its heartbeat.

Short on purpose: see `workers/heartbeat.WorkerHeartbeat`. Writing the file is
cheap, and the health check's limit does not depend on it.
"""
SCHEDULER_LOOP_SECONDS = 30
"""The longest Beat waits between scheduling passes, each of which writes its heartbeat."""
MAX_HEARTBEAT_AGE_SECONDS = 120
"""How old a heartbeat may be before the process counts as stuck."""


class Process(StrEnum):
    """Name the processes that write heartbeats."""

    WORKER = "worker"
    SCHEDULER = "scheduler"


def heartbeat_path(process: Process) -> Path:
    """Return the file `process` writes its heartbeat to."""
    return HEARTBEAT_DIRECTORY / f"sabe-{process.value}-heartbeat"


def write_heartbeat(process: Process) -> None:
    """Record that `process` is running now."""
    heartbeat_path(process).touch()


def heartbeat_age(path: Path, *, now: float) -> float | None:
    """Return how many seconds ago `path` was written, or `None` if it does not exist."""
    try:
        return now - path.stat().st_mtime
    except FileNotFoundError:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    """Check one process's heartbeat, reporting a problem on standard error.

    Args:
        argv: The process name and options; the command line's arguments if omitted.

    Returns:
        0 if the heartbeat is recent enough, otherwise 1.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("process", type=Process, choices=list(Process))
    parser.add_argument(
        "--max-age",
        type=float,
        default=MAX_HEARTBEAT_AGE_SECONDS,
        help="seconds a heartbeat may be old (default: %(default)s)",
    )
    arguments = parser.parse_args(argv)
    path = heartbeat_path(arguments.process)
    age = heartbeat_age(path, now=time.time())
    if age is None:
        print(f"unhealthy: no heartbeat at {path}", file=sys.stderr)
        return 1
    if age > arguments.max_age:
        print(
            f"unhealthy: last heartbeat {age:.0f} seconds ago "
            f"(limit {arguments.max_age:.0f})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
