"""Construct database-backed services for one worker operation."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

import httpx2
from sqlalchemy import Engine

from standard_annotation_backend.config import Settings, get_settings
from standard_annotation_backend.domain.ontology import OntologyKey
from standard_annotation_backend.ontology.registry import OntologyRegistry
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
from standard_annotation_backend.services.ontology_load_service import (
    OntologyLoadService,
)


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Group the settings, database engine, and services used by one task."""

    settings: Settings
    engine: Engine
    jobs: JobService
    authorization_sync: AuthorizationSyncService
    ontology_registry: OntologyRegistry
    ontology_loads: Mapping[OntologyKey, OntologyLoadService]


@contextmanager
def worker_runtime() -> Iterator[WorkerRuntime]:
    """Create a worker runtime and dispose its database engine afterward."""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    unit_of_work_factory = create_unit_of_work_factory(create_session_factory(engine))
    try:
        with httpx2.Client() as ontology_client:
            ontology_registry = OntologyRegistry(
                settings.ontology_sources,
                client_factory=lambda: ontology_client,
            )
            yield WorkerRuntime(
                settings=settings,
                engine=engine,
                jobs=JobService(unit_of_work_factory),
                authorization_sync=AuthorizationSyncService(unit_of_work_factory),
                ontology_registry=ontology_registry,
                ontology_loads={
                    key: OntologyLoadService(
                        unit_of_work_factory, ontology_registry.definition(key)
                    )
                    for key in ontology_registry.keys
                },
            )
    finally:
        engine.dispose()
