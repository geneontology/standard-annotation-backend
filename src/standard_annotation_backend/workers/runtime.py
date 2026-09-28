"""Construct database-backed services for one worker operation."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import Engine

from standard_annotation_backend.config import Settings, get_settings
from standard_annotation_backend.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from standard_annotation_backend.persistence.unit_of_work import (
    create_unit_of_work_factory,
)
from standard_annotation_backend.services.authorization_sync_service import (
    AuthorizationSyncService,
)
from standard_annotation_backend.services.job_service import JobService


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Group the settings, database engine, and services used by one task."""

    settings: Settings
    engine: Engine
    jobs: JobService
    authorization_sync: AuthorizationSyncService


@contextmanager
def worker_runtime() -> Iterator[WorkerRuntime]:
    """Create a worker runtime and dispose its database engine afterward."""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    unit_of_work_factory = create_unit_of_work_factory(create_session_factory(engine))
    try:
        yield WorkerRuntime(
            settings=settings,
            engine=engine,
            jobs=JobService(unit_of_work_factory),
            authorization_sync=AuthorizationSyncService(unit_of_work_factory),
        )
    finally:
        engine.dispose()
