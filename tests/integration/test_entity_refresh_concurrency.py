"""Verify entity catalog activation across independent connections."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic
from typing import Any, cast

import pytest
from refresh_helpers import FakeFetchers, build_runner, gpi_bytes
from seeding import create_job, entity_identifiers
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker
from test_entity_refresh_service import active_ids, refresh_catalog

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.repositories.entities import (
    EntityRepository,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import JobService

TIMEOUT = 10


def wait_for_lock(engine: Engine, backend_pid: int) -> None:
    """Wait until PostgreSQL reports that a backend process is waiting on a lock.

    Args:
        engine: Engine used to query `pg_stat_activity` on a separate connection.
        backend_pid: Process ID of the connection expected to be waiting.

    Raises:
        AssertionError: If no lock wait is seen before the timeout.
    """
    deadline = monotonic() + TIMEOUT
    with engine.connect() as connection:
        while monotonic() < deadline:
            if connection.scalar(
                text(
                    "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                ),
                {"pid": backend_pid},
            ):
                return
    raise AssertionError("second connection did not wait on a PostgreSQL lock")


def test_cross_source_race_leaves_a_shared_identifier_with_one_source(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sources activating one new identifier at once leave it with one source.

    Both jobs store their catalogs and hold their transactions open before either
    activates. Both succeed: the first to activate keeps the shared identifier, and
    the other rejects it as a collision and activates its own identifier.
    """
    service = EntityRefreshService()
    prefixes = {"mgi": "MGI", "rgd": "RGD"}
    for key, prefix in prefixes.items():
        refresh_catalog(service, unit_of_work_factory, f"{prefix}:old", source=key)
    runner = build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers(
            {
                key: gpi_bytes("SHARED:1", f"{prefix}:new")
                for key, prefix in prefixes.items()
            }
        ),
    )
    jobs = {
        key: create_job(
            unit_of_work_factory,
            job_type=JobType.ENTITY_REFRESH,
            requested_by="curator",
            parameters={"source_key": key},
        ).job_id
        for key in prefixes
    }
    both_stored = Barrier(2)
    original_lock_activation = EntityRepository.lock_activation

    def activate_together(self: EntityRepository) -> None:
        both_stored.wait(TIMEOUT)
        original_lock_activation(self)

    monkeypatch.setattr(EntityRepository, "lock_activation", activate_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(runner.run, jobs.values()))

    finished = {
        key: JobService(unit_of_work_factory).find(job_id)
        for key, job_id in jobs.items()
    }
    assert all(job.status is JobStatus.SUCCEEDED for job in finished.values())
    winner = next(
        key
        for key in prefixes
        if "SHARED:1" in entity_identifiers(session_factory, key)
    )
    loser = next(key for key in prefixes if key != winner)
    assert entity_identifiers(session_factory, winner) == {
        "SHARED:1",
        f"{prefixes[winner]}:new",
    }
    assert entity_identifiers(session_factory, loser) == {f"{prefixes[loser]}:new"}
    loser_result = finished[loser].result
    assert loser_result is not None
    report = cast(dict[str, Any], loser_result["rejection_report"])
    assert report["by_code"] == {"collision": 1}
    assert active_ids(session_factory) == sorted(
        [f"{prefixes[winner]}:new", f"{prefixes[loser]}:new", "SHARED:1"]
    )
