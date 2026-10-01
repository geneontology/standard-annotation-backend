"""Verify entity catalog replacement and validation across independent connections."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic
from uuid import UUID

from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from test_change_set_workflows import PROPOSER
from test_entity_refresh_service import active_ids, stage

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.entities import EntityCatalogCollisionError
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_service import AnnotationService
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)

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


def wait_for_blocked_by(engine: Engine, holder_pid: int) -> None:
    """Wait until another connection is waiting on a lock held by `holder_pid`.

    Use this when the waiting connection belongs to a service and its process ID
    is not available to the test.

    Args:
        engine: Engine used to query `pg_stat_activity` on a separate connection.
        holder_pid: Process ID of the connection that holds the lock.

    Raises:
        AssertionError: If no connection waits on `holder_pid` before the timeout.
    """
    deadline = monotonic() + TIMEOUT
    with engine.connect() as connection:
        while monotonic() < deadline:
            if connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE :holder = ANY(pg_blocking_pids(pid))"
                ),
                {"holder": holder_pid},
            ):
                return
    raise AssertionError("no connection waited on the lock holder")


def test_same_source_publications_serialize_and_readers_see_complete_catalogs(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A second publication for a source waits for the first to commit.

    Until then, readers still see the previous catalog. Afterward, the second
    publication's catalog is the active one.
    """
    service = EntityRefreshService(unit_of_work_factory)
    old = stage(service, session_factory, "MGI:old1", "MGI:old2")
    service.publish(job_id=old, actor_id="curator")
    first = stage(service, session_factory, "MGI:first1", "MGI:first2")
    second = stage(service, session_factory, "MGI:second1", "MGI:second2")
    entered = Event()
    pids: list[int] = []

    def publish_second() -> None:
        with session_factory() as session:
            pids.append(session.scalar(text("SELECT pg_backend_pid()")))
            entered.set()
            from standard_annotation_backend.persistence.repositories.entities import (
                EntityRepository,
            )

            EntityRepository(session).publish(second)
            session.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with unit_of_work_factory() as uow:
            uow.entities.publish(first)
            future = pool.submit(publish_second)
            assert entered.wait(TIMEOUT)
            wait_for_lock(database_engine, pids[0])
            assert active_ids(session_factory) == ["MGI:old1", "MGI:old2"]
            uow.commit()
        future.result(TIMEOUT)
    assert active_ids(session_factory) == ["MGI:second1", "MGI:second2"]


def test_different_nonconflicting_sources_publish_without_waiting(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Publishing one source does not block a source with distinct identifiers.

    The second source commits while the first source's transaction is still open.
    """
    service = EntityRefreshService(unit_of_work_factory)
    first = stage(service, session_factory, "MGI:1")
    other = stage(service, session_factory, "RGD:1", source="rgd")
    with ThreadPoolExecutor(max_workers=1) as pool, unit_of_work_factory() as uow:
        uow.entities.publish(first)
        result = pool.submit(service.publish, job_id=other, actor_id="curator").result(
            TIMEOUT
        )
        assert result.source_key == "rgd"
        assert active_ids(session_factory) == ["RGD:1"]
        uow.commit()
    assert active_ids(session_factory) == ["MGI:1", "RGD:1"]


def test_cross_source_membership_race_has_one_complete_winner(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """Two sources publishing the same identifier at once produce exactly one winner.

    The other publication fails with a collision and its source keeps its previous
    catalog.
    """
    service = EntityRefreshService(unit_of_work_factory)
    prefixes = {"mgi": "MGI", "rgd": "RGD"}
    jobs: dict[str, UUID] = {}
    for key, prefix in prefixes.items():
        old = stage(service, session_factory, f"{prefix}:old", source=key)
        service.publish(job_id=old, actor_id="curator")
        jobs[key] = stage(
            service, session_factory, "SHARED:1", f"{prefix}:new", source=key
        )
    barrier = Barrier(2)

    def synchronize_membership_insert(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.startswith("INSERT INTO entity_membership"):
            barrier.wait(TIMEOUT)

    def publish(key: str) -> tuple[str, bool]:
        try:
            service.publish(job_id=jobs[key], actor_id="curator")
        except EntityCatalogCollisionError:
            return key, False
        return key, True

    event.listen(
        database_engine, "before_cursor_execute", synchronize_membership_insert
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = dict(pool.map(publish, ("mgi", "rgd")))
    finally:
        event.remove(
            database_engine, "before_cursor_execute", synchronize_membership_insert
        )
    assert sum(outcomes.values()) == 1
    winner = next(key for key, won in outcomes.items() if won)
    loser = next(key for key, won in outcomes.items() if not won)
    assert active_ids(session_factory) == sorted(
        [f"{prefixes[winner]}:new", f"{prefixes[loser]}:old", "SHARED:1"]
    )
    assert service.completed(jobs[winner]) is not None
    assert service.completed(jobs[loser]) is None


def test_waiting_validation_resolves_the_replacement_of_a_retained_identifier(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    validated_annotation: Annotation,
) -> None:
    """An annotation create that waits on a catalog replacement still succeeds.

    When the replacement keeps the annotation's subject, the waiting create sees
    the new catalog after it commits and creates version 1 of the annotation.
    """
    service = EntityRefreshService(unit_of_work_factory)
    old = stage(service, session_factory, "MGI:1")
    service.publish(job_id=old, actor_id="curator")
    replacement = stage(service, session_factory, "MGI:1", "MGI:2")
    annotations = AnnotationService(unit_of_work_factory)
    payload = {**validated_annotation.model_dump(mode="json"), "db_object_id": "MGI:1"}

    with ThreadPoolExecutor(max_workers=1) as pool:
        with unit_of_work_factory() as uow:
            uow.entities.publish(replacement)
            holder_pid = uow.entities.session.scalar(text("SELECT pg_backend_pid()"))
            write = pool.submit(
                annotations.create,
                payload=payload,
                owning_group_id="other-group",
                context=PROPOSER,
            )
            wait_for_blocked_by(database_engine, holder_pid)
            uow.commit()
        created = write.result(TIMEOUT)

    assert created.version == 1
    assert created.annotation.db_object_id == "MGI:1"
    assert active_ids(session_factory) == ["MGI:1", "MGI:2"]
