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
from standard_annotation_backend.persistence.unit_of_work import (
    UnitOfWorkFactory,
    create_unit_of_work_factory,
)
from standard_annotation_backend.refresh.authorization import (
    AuthorizationRefreshKind,
)
from standard_annotation_backend.refresh.entity import EntityRefreshKind
from standard_annotation_backend.refresh.fetchers import SourceFetcher, SourceFetchers
from standard_annotation_backend.refresh.ontology import OntologyRefreshKind
from standard_annotation_backend.refresh.runner import RefreshRunner
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.authorization_refresh_service import (
    AuthorizationRefreshService,
)
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import JobService


@dataclass(frozen=True, slots=True)
class RefreshComponents:
    """Group the runner with the ontology kind that callers use directly.

    Attributes:
        ontology: The ontology kind, whose `prune` the prune task calls.
        runner: Runs jobs of every kind.
    """

    ontology: OntologyRefreshKind
    runner: RefreshRunner


def create_refresh_runner(
    *,
    engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    sources: RefreshSources,
    fetchers: SourceFetcher,
    enqueue_prune: Callable[[str], None] | None = None,
) -> RefreshComponents:
    """Build every refresh kind and a runner that can run all of them.

    Args:
        engine: Engine for advisory locks held on dedicated connections.
        unit_of_work_factory: Opens transactions for services.
        sources: Configured sources for every kind.
        fetchers: Fetches sources.
        enqueue_prune: Schedules ontology pruning after an ontology job finishes.
            `None` prunes in the calling process instead (used by the CLI).
    """
    jobs = JobService(unit_of_work_factory)
    authorization = AuthorizationRefreshKind(
        sources=sources,
        service=AuthorizationRefreshService(unit_of_work_factory),
        jobs=jobs,
    )
    entity = EntityRefreshKind(
        sources=sources, service=EntityRefreshService(unit_of_work_factory), jobs=jobs
    )
    ontology = OntologyRefreshKind(
        engine=engine,
        sources=sources,
        unit_of_work_factory=unit_of_work_factory,
        jobs=jobs,
        enqueue_prune=enqueue_prune,
    )
    return RefreshComponents(
        ontology=ontology,
        runner=RefreshRunner(
            engine=engine,
            jobs=jobs,
            fetchers=fetchers,
            kinds=(authorization, entity, ontology),
            retirer=entity,
        ),
    )


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Group the settings and services used by one Celery task or CLI run."""

    settings: Settings
    unit_of_work_factory: UnitOfWorkFactory
    jobs: JobService
    ontology: OntologyRefreshKind
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
