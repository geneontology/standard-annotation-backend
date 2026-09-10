"""Tests for creating application database engines and sessions."""

from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from standard_annotation_backend.persistence.database import (
    create_database_engine,
    create_session_factory,
)


def test_create_database_engine_uses_postgresql_defaults() -> None:
    """The engine uses the connection checks and isolation level we require."""
    with patch(
        "standard_annotation_backend.persistence.database.create_engine"
    ) as factory:
        expected_engine = object()
        factory.return_value = expected_engine

        result = create_database_engine("postgresql+psycopg://localhost/sab")

    factory.assert_called_once_with(
        "postgresql+psycopg://localhost/sab",
        isolation_level="READ COMMITTED",
        pool_pre_ping=True,
    )
    assert result is expected_engine


def test_create_session_factory_binds_engine_without_implicit_flush_or_expiry() -> None:
    """Sessions wait for explicit flushes and retain values after commits."""
    engine = create_engine("sqlite://")

    factory = create_session_factory(engine)

    assert isinstance(factory(), Session)
    assert factory.kw["bind"] is engine
    assert factory.kw["autoflush"] is False
    assert factory.kw["expire_on_commit"] is False
