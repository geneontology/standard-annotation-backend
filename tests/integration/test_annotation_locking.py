"""Tests that concurrent annotation writes coordinate safely in PostgreSQL."""

from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import Barrier
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.persistence.locks import (
    acquire_signature_locks,
    signature_lock_key,
)

LOW_SIGNATURE = "0123456789abcdef" * 4
HIGH_SIGNATURE = "f123456789abcdef" * 4
FAILURE_GUARD_SECONDS = 5
WORKER_DATABASE_TIMEOUT = "4s"


def _wait_for_futures(*futures: Future[Any]) -> None:
    _done, not_done = wait(futures, timeout=FAILURE_GUARD_SECONDS)
    assert not not_done, "concurrent database workers did not finish within 5 seconds"
    for future in futures:
        future.result()


def _set_worker_database_timeouts(session: Session) -> None:
    session.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": WORKER_DATABASE_TIMEOUT},
    )
    session.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)"),
        {"timeout": WORKER_DATABASE_TIMEOUT},
    )


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
