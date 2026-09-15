"""Protect worker cleanup used by change-set concurrency integration tests.

This is intentionally a test of test infrastructure, not application behavior.
The concurrency tests run database operations in worker threads, so failed cleanup
could hang the suite or let destructive fixture teardown begin while workers still
hold database connections. The probes run in child processes because the final
cleanup safeguard terminates the process when workers cannot be stopped.
"""

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

_PREAMBLE = """
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

from integration import test_change_set_concurrency as race

race.GUARD_SECONDS = 0.1
started = Event()
release = Event()
stopped = Event()
worker_threads = []

def worker():
    worker_threads.append(current_thread())
    started.set()
    release.wait()
    stopped.set()
    return "released"

executor = ThreadPoolExecutor(max_workers=1)
future = executor.submit(worker)
assert started.wait(1)
"""


def _run_probe(body: str) -> subprocess.CompletedProcess[str]:
    """Contain a failed drain regression in a child with an independent timeout."""
    return subprocess.run(
        [sys.executable, "-c", dedent(_PREAMBLE) + dedent(body)],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_overdue_cleanup_signals_and_joins_a_releasable_worker() -> None:
    """An overdue worker receives cleanup and stops before the failure is raised."""
    result = _run_probe(
        """
        cleanup_called = Event()

        def stop_workers():
            cleanup_called.set()
            release.set()

        try:
            race._drain_workers(executor, [future], [], stop_workers=stop_workers)
        except AssertionError as error:
            assert "deadline" in str(error)
            assert cleanup_called.is_set()
            assert stopped.is_set()
            assert future.done()
            assert future.result() == "released"
            assert not worker_threads[0].is_alive()
        else:
            raise AssertionError("overdue worker did not report its missed deadline")
        finally:
            # This also prevents a broken callback from leaking this probe's thread.
            release.set()
            executor.shutdown(wait=True)

        """
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("stall_cleanup", [False, True], ids=["worker", "cleanup"])
def test_unresponsive_worker_terminates_runner_before_fixture_teardown(
    stall_cleanup: bool,
) -> None:
    """A worker or cleanup stall stops the runner within a finite outer deadline."""
    body = (
        """
        def stop_workers():
            print("cleanup invoked", flush=True)
            Event().wait()

        try:
            race._drain_workers(executor, [future], [], stop_workers=stop_workers)
        finally:
            print("fixture teardown reached", flush=True)
        """
        if stall_cleanup
        else """
        try:
            race._drain_workers(executor, [future], [])
        finally:
            print("fixture teardown reached", flush=True)
        """
    )
    result = _run_probe(body)
    assert result.returncode == 1, result.stderr
    assert "database worker cleanup exceeded its deadline" in result.stderr
    assert "fixture teardown reached" not in result.stdout
    if stall_cleanup:
        assert "cleanup invoked" in result.stdout
