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
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshService,
)
from standard_annotation_backend.services.qc_report_service import QcReportRunner
from standard_annotation_backend.services.rules_refresh_service import (
    RulesRefreshService,
)
from standard_annotation_backend.services.term_replacement_service import (
    TermReplacementService,
)


@dataclass(frozen=True, slots=True)
class RefreshComponents:
    """Group the runner with the services that callers use directly.

    Attributes:
        runner: Runs jobs of every kind.
        term_replacement: Runs term replacement jobs.
        qc_report: Runs QC report jobs.
    """

    runner: RefreshRunner
    term_replacement: TermReplacementService
    qc_report: QcReportRunner


def create_refresh_runner(
    *,
    engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    sources: RefreshSources,
    fetchers: SourceFetcher,
    dispatch_replacement: Callable[[Job], None] | None = None,
) -> RefreshComponents:
    """Build the refresh services and runner, and the term replacement and QC runners.

    Args:
        engine: Engine for advisory locks held on dedicated connections.
        unit_of_work_factory: Opens transactions for services.
        sources: Configured sources for every kind.
        fetchers: Fetches sources.
        dispatch_replacement: Receives each job that a refresh queued, such as a
            term replacement job, after the refresh commits. Celery tasks send
            the job to a worker; the CLI collects the jobs and runs them
            afterward. `None` leaves the jobs queued.
    """
    try_lock = bind_try_lock(engine)
    ontologies = sources.ontology_catalog()
    jobs = JobService(unit_of_work_factory)
    return RefreshComponents(
        term_replacement=TermReplacementService(
            unit_of_work_factory, ontologies, try_lock
        ),
        qc_report=QcReportRunner(unit_of_work_factory, ontologies, try_lock),
        runner=RefreshRunner(
            try_lock=try_lock,
            jobs=jobs,
            unit_of_work_factory=unit_of_work_factory,
            fetchers=fetchers,
            sources=sources,
            kinds=(
                AuthorizationRefreshService(),
                RulesRefreshService(ontologies),
                EntityRefreshService(sources),
                OntologyRefreshService(sources),
                AnnotationRefreshService(sources),
            ),
            dispatch_follow_up=dispatch_replacement,
        ),
    )


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Group the settings and services used by one Celery task or CLI run."""

    settings: Settings
    unit_of_work_factory: UnitOfWorkFactory
    jobs: JobService
    runner: RefreshRunner
    term_replacement: TermReplacementService
    qc_report: QcReportRunner


@contextmanager
def worker_runtime(
    *,
    dispatch_replacement: Callable[[Job], None] | None = None,
) -> Iterator[WorkerRuntime]:
    """Create a worker runtime and dispose its database engine afterward.

    Args:
        dispatch_replacement: Receives each job that a refresh queued, such as a
            term replacement job. See `create_refresh_runner`.
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
                dispatch_replacement=dispatch_replacement,
            )
            yield WorkerRuntime(
                settings=settings,
                unit_of_work_factory=unit_of_work_factory,
                jobs=JobService(unit_of_work_factory),
                runner=refresh.runner,
                term_replacement=refresh.term_replacement,
                qc_report=refresh.qc_report,
            )
    finally:
        engine.dispose()
