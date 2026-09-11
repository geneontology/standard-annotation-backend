"""Set up a disposable PostgreSQL database for integration tests."""

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from sqlalchemy import Engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from standard_annotation_backend.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    create_unit_of_work_factory,
)

APPLICATION_TABLES = (
    "annotation_comment",
    "annotation_duplicate_reference",
    "annotation_multivalued_field_value",
    "annotation_version",
    "audit_event",
    "annotation",
    "job",
)


def _test_database_url() -> URL:
    raw_url = os.getenv("SAB_TEST_DATABASE_URL")
    if raw_url is None:
        pytest.skip(
            "set SAB_TEST_DATABASE_URL to a dedicated PostgreSQL database ending "
            "in '_test' to run integration tests",
            allow_module_level=True,
        )

    database_url = make_url(raw_url)
    if os.getenv("SAB_ENVIRONMENT") != "testing":
        raise pytest.UsageError(
            "integration database setup requires SAB_ENVIRONMENT=testing"
        )
    if "dbname" in database_url.query:
        raise pytest.UsageError(
            "SAB_TEST_DATABASE_URL must not include the 'dbname' query parameter "
            "because it overrides the guarded database path"
        )
    if not database_url.database or not database_url.database.endswith("_test"):
        raise pytest.UsageError(
            "SAB_TEST_DATABASE_URL must target a database whose name ends in '_test'"
        )
    return database_url


def _create_test_database_if_missing(database_url: URL) -> None:
    database_name = database_url.database
    if database_name is None:
        raise pytest.UsageError("SAB_TEST_DATABASE_URL must include a database name")

    connection_arguments = database_url.translate_connect_args(
        database="dbname", username="user"
    )
    connection_arguments.update(database_url.query)
    connection_arguments["dbname"] = "postgres"

    with (
        psycopg.connect(**connection_arguments, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database_name,),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
            )


@pytest.fixture(scope="session")
def database_engine() -> Iterator[Engine]:
    """Provide an engine connected to a freshly migrated test database.

    Yields:
        An engine connected to the database named by ``SAB_TEST_DATABASE_URL``.
    """
    database_url = _test_database_url()
    _create_test_database_if_missing(database_url)

    project_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(project_root / "alembic.ini"))
    rendered_url = database_url.render_as_string(False)
    alembic_config.set_main_option("sqlalchemy.url", rendered_url.replace("%", "%%"))
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    engine = create_database_engine(rendered_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(database_engine: Engine) -> sessionmaker[Session]:
    """Create sessions for the integration-test database.

    Args:
        database_engine: Engine connected to the migrated test database.

    Returns:
        The session factory used by repository tests.
    """
    return create_session_factory(database_engine)


@pytest.fixture
def unit_of_work_factory(
    session_factory: sessionmaker[Session],
) -> Callable[[], SqlAlchemyUnitOfWork]:
    """Create units of work for integration tests.

    Args:
        session_factory: Factory for test database sessions.

    Returns:
        A factory that creates a new unit of work for each test operation.
    """
    return create_unit_of_work_factory(session_factory)


@pytest.fixture(autouse=True)
def truncate_application_tables(database_engine: Engine) -> Iterator[None]:
    """Remove application rows after each integration test.

    Args:
        database_engine: Engine connected to the test database.

    Yields:
        Control to the test before clearing its rows.
    """
    yield
    with database_engine.begin() as connection:
        table_list = ", ".join(f'"{table}"' for table in APPLICATION_TABLES)
        connection.exec_driver_sql(f"TRUNCATE TABLE {table_list} CASCADE")
