"""Verify namespaced advisory locks across independent PostgreSQL connections."""

from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    acquire_transaction_lock,
    bind_try_lock,
    lock_key,
    try_advisory_lock,
)

_JOB_ID = UUID("00000000-0000-0000-0000-000000000027")


def _available_elsewhere(
    engine: Engine, namespace: LockNamespace, value: str | UUID | None
) -> bool:
    """Return whether a separate connection can take the lock right now."""
    with engine.connect() as connection, connection.begin():
        return bool(
            connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
                {"lock_key": lock_key(namespace, value)},
            )
        )


def test_try_lock_excludes_other_connections_until_the_context_exits(
    database_engine: Engine,
) -> None:
    """Only one connection holds a try-lock, and it is free again afterward."""
    with try_advisory_lock(database_engine, LockNamespace.JOB, _JOB_ID) as acquired:
        assert acquired is True
        with try_advisory_lock(
            database_engine, LockNamespace.JOB, _JOB_ID
        ) as competing:
            assert competing is False
    with try_advisory_lock(database_engine, LockNamespace.JOB, _JOB_ID) as recovered:
        assert recovered is True


def test_try_lock_does_not_block_other_values_or_namespaces(
    database_engine: Engine,
) -> None:
    """A held try-lock leaves other values and other namespaces available."""
    with try_advisory_lock(database_engine, LockNamespace.ONTOLOGY, "go") as acquired:
        assert acquired is True
        with try_advisory_lock(
            database_engine, LockNamespace.ONTOLOGY, "future-ontology"
        ) as other_value:
            assert other_value is True
        with try_advisory_lock(
            database_engine, LockNamespace.ANNOTATION_GROUP, "go"
        ) as other_namespace:
            assert other_namespace is True


def test_try_lock_is_released_when_the_context_raises(
    database_engine: Engine,
) -> None:
    """An error inside the context still releases the lock."""
    with (
        pytest.raises(RuntimeError, match="injected"),
        try_advisory_lock(
            database_engine, LockNamespace.ANNOTATION_GROUP, "mgi"
        ) as acquired,
    ):
        assert acquired is True
        raise RuntimeError("injected")
    assert _available_elsewhere(database_engine, LockNamespace.ANNOTATION_GROUP, "mgi")


def test_transaction_lock_is_held_until_the_transaction_ends(
    database_engine: Engine,
) -> None:
    """A transaction lock excludes other connections until commit or rollback."""
    for finish in (Session.commit, Session.rollback):
        with Session(database_engine) as session:
            acquire_transaction_lock(session, LockNamespace.REFRESH_START)
            assert not _available_elsewhere(
                database_engine, LockNamespace.REFRESH_START, None
            )
            assert _available_elsewhere(
                database_engine, LockNamespace.AUTHORIZATION_REFRESH, None
            )
            finish(session)
            assert _available_elsewhere(
                database_engine, LockNamespace.REFRESH_START, None
            )


def test_bound_try_lock_excludes_the_unbound_helper(database_engine: Engine) -> None:
    """A try-lock bound to an engine shares keys with `try_advisory_lock`."""
    try_lock = bind_try_lock(database_engine)
    with try_lock(LockNamespace.ONTOLOGY, "go") as acquired:
        assert acquired is True
        with try_advisory_lock(
            database_engine, LockNamespace.ONTOLOGY, "go"
        ) as competing:
            assert competing is False
