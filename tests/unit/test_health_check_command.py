"""Verify the health check command for worker and scheduler containers."""

import os
import time
from pathlib import Path

import pytest

from standard_annotation_backend.workers import health


@pytest.fixture(autouse=True)
def heartbeat_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep heartbeat files in a temporary directory."""
    monkeypatch.setattr(health, "HEARTBEAT_DIRECTORY", tmp_path)
    return tmp_path


def _beat(process: health.Process, *, seconds_ago: float) -> None:
    path = health.heartbeat_path(process)
    path.touch()
    then = time.time() - seconds_ago
    os.utime(path, (then, then))


@pytest.mark.parametrize("process", list(health.Process))
def test_a_recent_heartbeat_is_healthy(process: health.Process) -> None:
    """A process whose heartbeat is younger than two minutes is healthy."""
    _beat(process, seconds_ago=100)

    assert health.main([process.value]) == 0


@pytest.mark.parametrize("process", list(health.Process))
def test_an_old_heartbeat_is_unhealthy(
    process: health.Process, capsys: pytest.CaptureFixture[str]
) -> None:
    """A process whose heartbeat is older than two minutes is unhealthy."""
    _beat(process, seconds_ago=130)

    assert health.main([process.value]) == 1
    assert "130" in capsys.readouterr().err


def test_a_missing_heartbeat_is_unhealthy(capsys: pytest.CaptureFixture[str]) -> None:
    """A process that has written no heartbeat is unhealthy."""
    assert health.main(["worker"]) == 1
    assert "no heartbeat" in capsys.readouterr().err


def test_max_age_sets_how_old_a_heartbeat_may_be() -> None:
    """A heartbeat older than `--max-age` is unhealthy, and a younger one healthy."""
    _beat(health.Process.SCHEDULER, seconds_ago=10)

    assert health.main(["scheduler", "--max-age", "5"]) == 1
    assert health.main(["scheduler", "--max-age", "20"]) == 0


def test_the_processes_keep_separate_heartbeats() -> None:
    """A worker heartbeat does not make the scheduler healthy."""
    _beat(health.Process.WORKER, seconds_ago=0)

    assert health.main(["worker"]) == 0
    assert health.main(["scheduler"]) == 1
