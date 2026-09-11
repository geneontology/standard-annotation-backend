"""Tests that concurrent annotation writes coordinate safely in PostgreSQL."""

from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import Barrier, Event
from time import monotonic
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence.locks import (
    acquire_signature_locks,
    signature_lock_key,
)
from standard_annotation_backend.persistence.models import AnnotationOrigin
from standard_annotation_backend.persistence.repositories import AnnotationRepository

LOW_SIGNATURE = "0123456789abcdef" * 4
HIGH_SIGNATURE = "f123456789abcdef" * 4
FAILURE_GUARD_SECONDS = 5
LOCK_OBSERVATION_SECONDS = 2
WORKER_DATABASE_TIMEOUT = "4s"


def _wait_for_futures(*futures: Future[Any]) -> None:
    _done, not_done = wait(futures, timeout=FAILURE_GUARD_SECONDS)
    assert not not_done, "concurrent database workers did not finish within 5 seconds"
    for future in futures:
        future.result()


def _wait_for_signature_lock_contention(
    observer: Session,
    *,
    signature: str,
    holder_pid: int,
    waiter_pid: int,
) -> None:
    lock_key = signature_lock_key(signature)
    class_id = lock_key >> 32
    object_id = lock_key & 0xFFFFFFFF
    expected_states = {(holder_pid, True), (waiter_pid, False)}
    last_states: set[tuple[int, bool]] = set()
    deadline = monotonic() + LOCK_OBSERVATION_SECONDS

    statement = text(
        """
        SELECT pid, granted
        FROM pg_locks
        WHERE locktype = 'advisory'
          AND mode = 'ExclusiveLock'
          AND classid = :class_id
          AND objid = :object_id
          AND objsubid = 1
          AND pid IN (:holder_pid, :waiter_pid)
        """
    )
    while monotonic() < deadline:
        last_states = {
            (row.pid, row.granted)
            for row in observer.execute(
                statement,
                {
                    "class_id": class_id,
                    "object_id": object_id,
                    "holder_pid": holder_pid,
                    "waiter_pid": waiter_pid,
                },
            )
        }
        if last_states == expected_states:
            return

    raise AssertionError(
        "PostgreSQL did not show the holder's granted advisory lock and the "
        f"waiter's ungranted request for key {lock_key}; observed {last_states}"
    )


def _worker_backend_pid(session: Session) -> int:
    backend_pid = session.scalar(text("SELECT pg_backend_pid()"))
    assert backend_pid is not None
    return backend_pid


def _set_worker_database_timeouts(session: Session) -> None:
    session.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": WORKER_DATABASE_TIMEOUT},
    )
    session.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)"),
        {"timeout": WORKER_DATABASE_TIMEOUT},
    )


def _set_observer_statement_timeout(session: Session) -> None:
    session.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)"),
        {"timeout": WORKER_DATABASE_TIMEOUT},
    )


def _drain_database_workers(
    executor: ThreadPoolExecutor,
    futures: tuple[Future[Any], ...],
) -> set[Future[Any]]:
    _done, overdue = wait(futures, timeout=FAILURE_GUARD_SECONDS)
    if overdue:
        # All worker database statements and event waits were bounded before
        # submission. Drain any deadline miss before sessions can be closed.
        wait(overdue)
    assert all(future.done() for future in futures)
    executor.shutdown(wait=True, cancel_futures=True)
    return overdue


def _changed_annotation(annotation: Annotation, **changes: object) -> Annotation:
    annotation_data = annotation.model_dump(mode="json")
    annotation_data.update(changes)
    return Annotation.model_validate(annotation_data)


def _create_annotation(
    session_factory: sessionmaker[Session],
    annotation: Annotation,
) -> UUID:
    with session_factory() as session:
        record = AnnotationRepository(session).create(
            annotation=annotation,
            actor_id="creator",
            change_source="api",
            owning_group_id="group-1",
            record_origin=AnnotationOrigin.DIRECT,
        )
        session.commit()
        return record.annotation_id


def test_signature_lock_is_released_when_its_transaction_commits(
    session_factory: sessionmaker[Session],
) -> None:
    lock_key = signature_lock_key(LOW_SIGNATURE)

    with session_factory() as holder, session_factory() as contender:
        acquire_signature_locks(holder, [LOW_SIGNATURE])

        acquired_while_held = contender.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
        contender.rollback()

        holder.commit()

        acquired_after_commit = contender.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
        contender.rollback()

    assert acquired_while_held is False
    assert acquired_after_commit is True


def test_waiter_reads_committed_update_after_acquiring_signature_lock(
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_annotation(session_factory, validated_annotation)
    changed = _changed_annotation(validated_annotation, assigned_by="Waited_Source")
    expected_annotation_data = changed.model_dump(mode="json")
    # assigned_by does not participate in the duplicate signature, so both writes
    # contend for the annotation's existing signature lock.
    with session_factory() as session:
        current = AnnotationRepository(session).get(annotation_id)
        assert current is not None
        signature = current.duplicate_base_signature

    update_flushed = Event()
    allow_update_commit = Event()
    waiter_attempting_lock = Event()
    waiter_acquired_lock = Event()

    def update_then_commit(session: Session) -> None:
        updated = AnnotationRepository(session).update(
            annotation_id,
            changed,
            actor_id="editor-a",
            change_source="api",
        )
        assert updated.annotation_data == expected_annotation_data
        update_flushed.set()
        if not allow_update_commit.wait(FAILURE_GUARD_SECONDS):
            raise TimeoutError("update worker was not released to commit")
        session.commit()

    def wait_then_read(session: Session) -> tuple[dict[str, object], str]:
        waiter_attempting_lock.set()
        acquire_signature_locks(session, [signature])
        waiter_acquired_lock.set()
        current = AnnotationRepository(session).get(annotation_id)
        assert current is not None
        return current.annotation_data, current.assigned_by

    with (
        session_factory() as holder_session,
        session_factory() as waiter_session,
        session_factory() as observer_session,
    ):
        _set_worker_database_timeouts(holder_session)
        holder_pid = _worker_backend_pid(holder_session)
        _set_worker_database_timeouts(waiter_session)
        waiter_pid = _worker_backend_pid(waiter_session)
        _set_observer_statement_timeout(observer_session)

        executor = ThreadPoolExecutor(max_workers=2)
        futures: tuple[Future[Any], ...] = ()
        primary_failure: BaseException | None = None
        try:
            update_future = executor.submit(update_then_commit, holder_session)
            futures = (update_future,)
            assert update_flushed.wait(FAILURE_GUARD_SECONDS)

            waiter_future = executor.submit(wait_then_read, waiter_session)
            futures = (update_future, waiter_future)
            assert waiter_attempting_lock.wait(FAILURE_GUARD_SECONDS)
            _wait_for_signature_lock_contention(
                observer_session,
                signature=signature,
                holder_pid=holder_pid,
                waiter_pid=waiter_pid,
            )
            assert not waiter_acquired_lock.is_set()
        except BaseException as error:
            primary_failure = error
        finally:
            allow_update_commit.set()
            overdue = _drain_database_workers(
                executor,
                futures,
            )

        failures: list[BaseException] = []
        if primary_failure is not None:
            failures.append(primary_failure)
        if overdue:
            failures.append(
                AssertionError("database workers did not terminate within 5 seconds")
            )
        for future in futures:
            if future.done() and not future.cancelled():
                try:
                    future.result()
                except BaseException as worker_error:
                    failures.append(worker_error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("coordination and worker failures", failures)

        observed_annotation_data, observed_assigned_by = waiter_future.result()

    assert observed_annotation_data == expected_annotation_data
    assert observed_annotation_data["assigned_by"] == "Waited_Source"
    assert observed_assigned_by == "Waited_Source"


def test_inverse_signature_requests_complete_without_deadlock(
    session_factory: sessionmaker[Session],
) -> None:
    start = Barrier(2)

    def acquire_and_commit(session: Session, signatures: list[str]) -> None:
        start.wait(timeout=FAILURE_GUARD_SECONDS)
        acquire_signature_locks(session, signatures)
        session.commit()

    with (
        session_factory() as forward_session,
        session_factory() as reverse_session,
    ):
        _set_worker_database_timeouts(forward_session)
        forward_session.execute(text("SET LOCAL lock_timeout = '2s'"))
        _set_worker_database_timeouts(reverse_session)
        reverse_session.execute(text("SET LOCAL lock_timeout = '2s'"))

        with ThreadPoolExecutor(max_workers=2) as executor:
            forward = executor.submit(
                acquire_and_commit,
                forward_session,
                [LOW_SIGNATURE, HIGH_SIGNATURE],
            )
            reverse = executor.submit(
                acquire_and_commit,
                reverse_session,
                [HIGH_SIGNATURE, LOW_SIGNATURE],
            )

            _wait_for_futures(forward, reverse)
