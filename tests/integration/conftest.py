"""Set up a disposable PostgreSQL database for integration tests."""

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from seeding import insert_annotation
from sqlalchemy import Engine, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from standard_annotation_backend.api.dependencies import (
    get_authenticated_context,
    get_unit_of_work_factory,
)
from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
)
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
    RequestContext,
)
from standard_annotation_backend.main import app
from standard_annotation_backend.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from standard_annotation_backend.persistence.models import (
    EntityCatalogSnapshotRecord,
    EntityMembershipRecord,
    JobRecord,
    SabGroupRecord,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
    create_unit_of_work_factory,
)


@pytest.fixture
def seed_active_subjects(
    session_factory: sessionmaker[Session],
) -> Callable[..., None]:
    """Return a helper that makes the given identifiers active in the entity catalog.

    The helper writes catalog rows directly, so it records no refresh audit events.
    """

    def seed(*identifiers: str) -> None:
        now = datetime.now(UTC)
        with session_factory() as session:
            job_id, snapshot_id = uuid4(), uuid4()
            source_key = f"seed-{uuid4().hex}"
            session.add(
                JobRecord(
                    job_id=job_id,
                    job_type="entity_refresh",
                    status="succeeded",
                    requested_by="test-supplier",
                    parameters={"source_key": source_key},
                    progress={},
                    warnings=[],
                    created_at=now,
                    updated_at=now,
                    started_at=now,
                    completed_at=now,
                    result={},
                )
            )
            session.flush()
            session.add(
                EntityCatalogSnapshotRecord(
                    snapshot_id=snapshot_id,
                    job_id=job_id,
                    source_key=source_key,
                    source_type="https",
                    source_locator="https://example.org/test.gpi",
                    source_checksum="a" * 64,
                    source_format="gpi-2.0",
                    source_metadata={},
                    source_statistics={},
                    record_statistics={},
                    fetched_at=now,
                    staged_at=now,
                    published_at=now,
                    active=True,
                )
            )
            session.flush()
            session.add_all(
                [
                    EntityMembershipRecord(
                        db_object_id=identifier,
                        source_key=source_key,
                        snapshot_id=snapshot_id,
                    )
                    for identifier in identifiers
                ]
            )
            session.commit()

    return seed


@pytest.fixture
def active_annotation_subjects(seed_active_subjects: Callable[..., None]) -> None:
    """Seed the subjects used by existing successful annotation workflow tests."""
    seed_active_subjects("UniProtKB:P12345", "UniProtKB:Q99999")


APPLICATION_TABLES = (
    "annotation_staging_duplicate_reference",
    "annotation_staging_multivalued_value",
    "annotation_staging",
    "group_annotation_management",
    "annotation_import",
    "entity_source_record",
    "entity_membership",
    "entity_staging_record",
    "entity_catalog_snapshot",
    "ontology_closure",
    "ontology_term",
    "ontology_metadata",
    "api_token",
    "token_management_session",
    "authorization_assignment",
    "authorization_refresh",
    "sab_user",
    "sab_group",
    "change_set",
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
def alembic_config() -> Config:
    """Return an Alembic configuration that targets the disposable test database."""
    database_url = _test_database_url()
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    rendered_url = database_url.render_as_string(False)
    config.set_main_option("sqlalchemy.url", rendered_url.replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def database_engine(alembic_config: Config) -> Iterator[Engine]:
    """Provide an engine connected to a freshly migrated test database.

    Yields:
        An engine connected to the database named by `SAB_TEST_DATABASE_URL`.
    """
    database_url = _test_database_url()
    _create_test_database_if_missing(database_url)
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    engine = create_database_engine(database_url.render_as_string(False))
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


@pytest.fixture
def integration_api_client(
    configured_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
) -> Iterator[TestClient]:
    """Provide an API client backed by the isolated integration-test database.

    Args:
        configured_environment: Fixture that supplies the required application settings.
        unit_of_work_factory: Callable that opens test database transactions.

    Yields:
        A client whose requests use the test database and a fixed test identity.
    """
    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_unit_of_work_factory] = lambda: unit_of_work_factory
    app.dependency_overrides[get_authenticated_context] = lambda: RequestContext(
        actor_id="api-test-user",
        token_id=UUID("00000000-0000-0000-0000-000000000501"),
        token_name="Integration tests",
        role=AuthorizationRole.ADMIN,
        scope=AuthorizationScope.GLOBAL,
        group_id=None,
    )
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


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


@pytest.fixture
def seed_annotation(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> Callable[[str], UUID]:
    """Provide a helper that creates one active direct annotation for a subject."""
    with session_factory() as session:
        if (
            session.scalar(
                select(SabGroupRecord).where(SabGroupRecord.group_key == "MGI")
            )
            is None
        ):
            session.add(SabGroupRecord(group_key="MGI"))
            session.commit()
    counter = iter(range(1, 1_000_000))

    def seed(db_object_id: str) -> UUID:
        annotation_id = uuid4()
        annotation = Annotation.model_validate(
            {
                "db_object_id": db_object_id,
                "relation": "RO:0002331",
                "ontology_class_id": "GO:0008150",
                "references": [f"PMID:{next(counter)}"],
                "evidence_type": "ECO:0000314",
                "annotation_date": "2026-09-29",
                "assigned_by": "MGI",
            }
        )
        with unit_of_work_factory() as uow:
            insert_annotation(
                uow.annotations.session,
                annotation=annotation,
                actor_id="curator",
                change_source="test",
                owning_group_id="MGI",
                record_origin=AnnotationOrigin.DIRECT,
                annotation_id=annotation_id,
            )
            uow.commit()
        return annotation_id

    return seed
