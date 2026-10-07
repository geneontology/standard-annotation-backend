"""Verify GPAD publication against independent PostgreSQL connections."""

from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event, Lock
from time import monotonic
from typing import Any
from uuid import UUID

import pytest
from annotation_refresh_helpers import gpad_bytes, gpad_row, gpad_text
from psycopg import Cursor
from refresh_helpers import TEST_SOURCES, ignore_progress, source_document
from sqlalchemy import Engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker
from test_annotation_refresh_service import GROUP, SOURCE, create_job, stage
from test_entity_refresh_concurrency import TIMEOUT, wait_for_lock

from standard_annotation_backend.domain.annotation_management import (
    AnnotationManagementMode,
    AnnotationRefreshResult,
    CutoverRejectedError,
    GroupSabManagedError,
)
from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.persistence.locks import (
    GLOBAL_ANNOTATION_WRITE_LOCK_KEY,
    bind_try_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationMultivaluedFieldValueRecord,
    AnnotationRecord,
    EntityMembershipRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_refresh_service import (
    AnnotationRefreshService,
)

_PUBLICATION_COPY_STATEMENT = (
    f"INSERT INTO {AnnotationMultivaluedFieldValueRecord.__tablename__}"
)
_STAGED_SUBJECT_CHECK_STATEMENT = "SELECT annotation_staging.line_number"


@dataclass(slots=True)
class PublicationPause:
    """Track a publication paused partway through copying staged annotations.

    Attributes:
        reached: Set once the first publication has deleted the group's old
            annotations and inserted the new ones, before it copies their lookup
            rows or commits.
        release: Set by the test to let the paused publication continue.
        publisher_pid: Backend process ID of the paused publication's connection.
        waiter_started: Set once another connection asks for the global
            annotation-write lock while the publication is paused.
        waiter_pid: Backend process ID of that other connection.
    """

    reached: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    publisher_pid: int | None = None
    waiter_started: Event = field(default_factory=Event)
    waiter_pid: int | None = None
    _guard: Lock = field(default_factory=Lock)


@contextmanager
def pause_first_publication(engine: Engine) -> Iterator[PublicationPause]:
    """Pause the first publication right after it inserts the new annotations.

    The publication is still inside its transaction when it pauses, so the test
    can observe what other connections see and whether they wait. Only the first
    publication pauses; later ones run normally. While it is paused, the first
    other connection to ask for the global annotation-write lock is recorded as
    the waiter. The pause is always released when the context exits.

    Yields:
        The pause state, including the backend process IDs it captured.
    """
    pause = PublicationPause()

    def before_cursor_execute(
        connection: object,
        cursor: Cursor[Any],
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        backend_pid = cursor.connection.info.backend_pid
        if (
            "pg_advisory_xact_lock" in statement
            and isinstance(parameters, Mapping)
            and parameters.get("lock_key") == GLOBAL_ANNOTATION_WRITE_LOCK_KEY
        ):
            if pause.reached.is_set() and backend_pid != pause.publisher_pid:
                pause.waiter_pid = backend_pid
                pause.waiter_started.set()
            return
        if not statement.startswith(_PUBLICATION_COPY_STATEMENT):
            return
        with pause._guard:
            if pause.reached.is_set():
                return
            pause.publisher_pid = backend_pid
            pause.reached.set()
        assert pause.release.wait(TIMEOUT)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield pause
    finally:
        pause.release.set()
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def wait_until_blocked_by(engine: Engine, waiter_pid: int, holder_pid: int) -> None:
    """Wait until PostgreSQL reports that `waiter_pid` waits on `holder_pid`.

    Raises:
        AssertionError: If the wait is not seen before the timeout.
    """
    deadline = monotonic() + TIMEOUT
    with engine.connect() as connection:
        while monotonic() < deadline:
            if connection.scalar(
                text("SELECT :holder = ANY(pg_blocking_pids(:waiter))"),
                {"holder": holder_pid, "waiter": waiter_pid},
            ):
                return
    raise AssertionError("connection did not wait on the paused publication")


def publish(
    unit_of_work_factory: UnitOfWorkFactory, job_id: UUID, actor_id: str
) -> AnnotationRefreshResult:
    """Publish a job's committed staging in its own transaction, without an audit."""
    with unit_of_work_factory() as uow:
        result = uow.annotation_imports.publish(job_id, actor_id=actor_id)
        uow.commit()
        return result


def group_mode(
    unit_of_work_factory: UnitOfWorkFactory, group_key: str = GROUP
) -> AnnotationManagementMode:
    """Return whether GPAD or SAB currently manages `group_key`."""
    with unit_of_work_factory() as uow:
        return uow.annotation_imports.group_mode(group_key)


def group_rows(
    session_factory: sessionmaker[Session], group_key: str = GROUP
) -> list[tuple[str, list[str]]]:
    """Return each group annotation's origin and references, in a stable order."""
    with session_factory() as session:
        rows = session.execute(
            select(AnnotationRecord.record_origin, AnnotationRecord.annotation_data)
            .where(AnnotationRecord.owning_group_id == group_key)
            .order_by(AnnotationRecord.annotation_data["references"].astext)
        ).all()
    return [(origin, list(data["references"])) for origin, data in rows]


def test_publication_hides_its_changes_and_makes_annotation_writes_wait(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
    seed_annotation: Callable[[str], UUID],
) -> None:
    """Until publication commits, readers see the group's complete old data.

    A direct annotation write, even for another group, waits on the publishing
    connection and completes only after the publication commits.
    """
    seed_active_subjects("UniProtKB:P12345", "UniProtKB:Q99999")
    seed_annotation("UniProtKB:Q99999")
    old_rows = group_rows(session_factory)
    assert [origin for origin, _ in old_rows] == ["direct"]
    service = AnnotationRefreshService(
        unit_of_work_factory, TEST_SOURCES, bind_try_lock(database_engine)
    )
    job = create_job(unit_of_work_factory)
    document = source_document(
        SOURCE,
        gpad_bytes(
            gpad_row("UniProtKB:P12345"),
            gpad_row("UniProtKB:P12345", reference="PMID:2"),
        ),
    )

    def write_other_group() -> None:
        with unit_of_work_factory() as uow:
            uow.annotations.create(
                annotation=Annotation.model_validate(
                    {
                        "db_object_id": "UniProtKB:Q99999",
                        "relation": "RO:0002331",
                        "ontology_class_id": "GO:0008150",
                        "references": ["PMID:42"],
                        "evidence_type": "ECO:0000314",
                        "annotation_date": "2026-09-29",
                        "assigned_by": "RGD",
                    }
                ),
                actor_id="curator",
                owning_group_id="RGD",
            )
            uow.commit()

    with (
        pause_first_publication(database_engine) as pause,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        published = pool.submit(service.apply, job, document, ignore_progress)
        assert pause.reached.wait(TIMEOUT)
        assert pause.publisher_pid is not None
        assert group_rows(session_factory) == old_rows

        writer = pool.submit(write_other_group)
        assert pause.waiter_started.wait(TIMEOUT)
        assert pause.waiter_pid is not None
        wait_for_lock(database_engine, pause.waiter_pid)
        wait_until_blocked_by(database_engine, pause.waiter_pid, pause.publisher_pid)
        assert not writer.done()
        assert group_rows(session_factory, "RGD") == []
        assert group_rows(session_factory) == old_rows

        pause.release.set()
        published.result(timeout=TIMEOUT)
        writer.result(timeout=TIMEOUT)

    assert group_rows(session_factory) == [
        ("import", ["PMID:1"]),
        ("import", ["PMID:2"]),
    ]
    assert group_rows(session_factory, "RGD") == [("direct", ["PMID:42"])]


def test_refresh_waiting_on_a_cutover_cannot_overwrite_the_sab_managed_group(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """An ordinary refresh staged before a cutover fails once the cutover commits.

    The refresh publication waits while the cutover publishes. After the cutover
    commits, the refresh rejects the now SAB-managed group with
    `GroupSabManagedError`, and the group keeps the cutover's annotations.
    """
    seed_active_subjects("UniProtKB:P12345")
    service = AnnotationRefreshService(
        unit_of_work_factory, TEST_SOURCES, bind_try_lock(database_engine)
    )
    refresh = create_job(unit_of_work_factory)
    cutover = create_job(unit_of_work_factory, JobType.ANNOTATION_CUTOVER)
    stage(service, refresh, gpad_text(gpad_row("UniProtKB:P12345", reference="PMID:1")))
    stage(service, cutover, gpad_text(gpad_row("UniProtKB:P12345", reference="PMID:2")))
    with (
        pause_first_publication(database_engine) as pause,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(publish, unit_of_work_factory, cutover.job_id, "admin")
        assert pause.reached.wait(TIMEOUT)
        assert pause.publisher_pid is not None
        second = pool.submit(publish, unit_of_work_factory, refresh.job_id, "scheduler")
        assert pause.waiter_started.wait(TIMEOUT)
        assert pause.waiter_pid is not None
        wait_until_blocked_by(database_engine, pause.waiter_pid, pause.publisher_pid)
        assert not second.done()
        pause.release.set()
        first.result(timeout=TIMEOUT)
        with pytest.raises(GroupSabManagedError):
            second.result(timeout=TIMEOUT)

    assert group_mode(unit_of_work_factory) is AnnotationManagementMode.SAB_MANAGED
    with unit_of_work_factory() as uow:
        assert uow.annotation_imports.published_result(refresh.job_id) is None
    assert group_rows(session_factory) == [("import", ["PMID:2"])]


@contextmanager
def pause_before_staged_subject_check(engine: Engine) -> Iterator[PublicationPause]:
    """Pause a cutover publication after it locks its subjects' entity rows.

    The pause comes just before the publication decides which staged subjects
    are missing, while its transaction is still open. The pause is always
    released when the context exits.

    Yields:
        The pause state; only `reached` and `release` are used.
    """
    pause = PublicationPause()

    def before_cursor_execute(
        connection: object,
        cursor: Cursor[Any],
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if not statement.startswith(_STAGED_SUBJECT_CHECK_STATEMENT):
            return
        with pause._guard:
            if pause.reached.is_set():
                return
            pause.reached.set()
        assert pause.release.wait(TIMEOUT)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield pause
    finally:
        pause.release.set()
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def test_cutover_rejects_a_subject_that_becomes_active_after_its_locks(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """A cutover accepts only subjects whose entity rows it locked.

    A subject that becomes active again after the cutover locked the others is
    rejected: its row is not locked, so an entity refresh could remove it again
    before the cutover commits. The group stays `gpad_imported`.
    """
    seed_active_subjects("UniProtKB:P12345", "UniProtKB:Q99999")
    service = AnnotationRefreshService(
        unit_of_work_factory, TEST_SOURCES, bind_try_lock(database_engine)
    )
    cutover = create_job(unit_of_work_factory, JobType.ANNOTATION_CUTOVER)
    stage(
        service,
        cutover,
        gpad_text(gpad_row("UniProtKB:P12345"), gpad_row("UniProtKB:Q99999")),
    )
    with session_factory() as session:
        removed = session.get(EntityMembershipRecord, "UniProtKB:Q99999")
        assert removed is not None
        source_key, snapshot_id = removed.source_key, removed.snapshot_id
        session.delete(removed)
        session.commit()

    with (
        pause_before_staged_subject_check(database_engine) as pause,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        published = pool.submit(publish, unit_of_work_factory, cutover.job_id, "admin")
        assert pause.reached.wait(TIMEOUT)
        with session_factory() as session:
            session.add(
                EntityMembershipRecord(
                    db_object_id="UniProtKB:Q99999",
                    source_key=source_key,
                    snapshot_id=snapshot_id,
                )
            )
            session.commit()
        pause.release.set()
        with pytest.raises(CutoverRejectedError) as raised:
            published.result(timeout=TIMEOUT)

    assert [
        (issue.line_number, issue.code) for issue in raised.value.report.issues
    ] == [(5, "unknown_db_object_id")]
    assert group_mode(unit_of_work_factory) is AnnotationManagementMode.GPAD_IMPORTED
    assert group_rows(session_factory) == []
