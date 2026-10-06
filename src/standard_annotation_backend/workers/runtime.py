"""Construct the services and refresh runner used by one Celery task or CLI run."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import httpx2
from sqlalchemy import Engine

from standard_annotation_backend.config import Settings, get_settings
from standard_annotation_backend.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from standard_annotation_backend.persistence.locks import bind_try_lock
from standard_annotation_backend.persistence.unit_of_work import (
    UnitOfWorkFactory,
    create_unit_of_work_factory,
)
from standard_annotation_backend.refresh.fetchers import SourceFetcher, SourceFetchers
from standard_annotation_backend.refresh.runner import RefreshRunner
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.annotation_refresh_service import (
    AnnotationRefreshService,
)
from standard_annotation_backend.services.authorization_refresh_service import (
    AuthorizationRefreshService,
)
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import JobService
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshService,
)


@dataclass(frozen=True, slots=True)
class RefreshComponents:
    """Group the runner with the ontology service that callers use directly.

    Attributes:
        ontology: The ontology service, whose `prune` the prune task calls.
        runner: Runs jobs of every kind.
    """

    ontology: OntologyRefreshService
    runner: RefreshRunner


def create_refresh_runner(
    *,
    engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    sources: RefreshSources,
    fetchers: SourceFetcher,
    enqueue_prune: Callable[[str], None] | None = None,
) -> RefreshComponents:
    """Build every refresh service and a runner that can run all of them.

    Args:
        engine: Engine for advisory locks held on dedicated connections.
        unit_of_work_factory: Opens transactions for services.
        sources: Configured sources for every kind.
        fetchers: Fetches sources.
        enqueue_prune: Schedules ontology pruning after an ontology job finishes.
            `None` prunes in the calling process instead (used by the CLI).
    """
    try_lock = bind_try_lock(engine)
    jobs = JobService(unit_of_work_factory)
    authorization = AuthorizationRefreshService(unit_of_work_factory)
    entity = EntityRefreshService(unit_of_work_factory, sources)
    ontology = OntologyRefreshService(unit_of_work_factory, try_lock, enqueue_prune)
    annotation = AnnotationRefreshService(unit_of_work_factory, sources, try_lock)
    return RefreshComponents(
        ontology=ontology,
        runner=RefreshRunner(
            try_lock=try_lock,
            jobs=jobs,
            fetchers=fetchers,
            sources=sources,
            kinds=(authorization, entity, ontology, annotation),
            retirer=entity,
        ),
    )


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Group the settings and services used by one Celery task or CLI run."""

    settings: Settings
    unit_of_work_factory: UnitOfWorkFactory
    jobs: JobService
    ontology: OntologyRefreshService
    runner: RefreshRunner


@contextmanager
def worker_runtime(
    *, enqueue_prune: Callable[[str], None] | None = None
) -> Iterator[WorkerRuntime]:
    """Create a worker runtime and dispose its database engine afterward.

    Args:
        enqueue_prune: Schedules ontology pruning after an ontology job ends.
            See `create_refresh_runner`.
    """
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    unit_of_work_factory = create_unit_of_work_factory(create_session_factory(engine))
    try:
        # One HTTP client serves every source fetched in this operation.
        with httpx2.Client() as source_client:
            fetchers = SourceFetchers(
                source_client,
                connect_timeout_seconds=settings.source_connect_timeout_seconds,
                read_timeout_seconds=settings.source_read_timeout_seconds,
            )
            refresh = create_refresh_runner(
                engine=engine,
                unit_of_work_factory=unit_of_work_factory,
                sources=settings.sources,
                fetchers=fetchers,
                enqueue_prune=enqueue_prune,
            )
            yield WorkerRuntime(
                settings=settings,
                unit_of_work_factory=unit_of_work_factory,
                jobs=JobService(unit_of_work_factory),
                ontology=refresh.ontology,
                runner=refresh.runner,
            )
    finally:
        engine.dispose()
