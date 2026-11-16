"""Verify entity catalog activation across independent connections."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic

import pytest
from refresh_helpers import TEST_SOURCES, FakeFetchers, build_runner, gpi_bytes
from seeding import create_job, entity_identifiers
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker
from test_entity_refresh_service import active_ids, publish_catalog

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


def test_cross_source_membership_race_has_one_complete_winner(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sources publishing the same identifier at once produce exactly one winner.

    Both jobs store their catalogs and hold their transactions open before either
    activates. The other job fails with a collision and its source keeps its
    previous catalog.
    """
    service = EntityRefreshService(TEST_SOURCES)
    prefixes = {"mgi": "MGI", "rgd": "RGD"}
    for key, prefix in prefixes.items():
        publish_catalog(service, unit_of_work_factory, f"{prefix}:old", source=key)
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
    winner = next(
        key for key, job in finished.items() if job.status is JobStatus.SUCCEEDED
    )
    loser = next(key for key, job in finished.items() if key != winner)
    assert finished[loser].status is JobStatus.FAILED
    assert finished[loser].progress["failure_code"] == "catalog_collision"
    assert entity_identifiers(session_factory, winner) == {
        "SHARED:1",
        f"{prefixes[winner]}:new",
    }
    assert entity_identifiers(session_factory, loser) == {f"{prefixes[loser]}:old"}
    assert active_ids(session_factory) == sorted(
        [f"{prefixes[winner]}:new", f"{prefixes[loser]}:old", "SHARED:1"]
    )
