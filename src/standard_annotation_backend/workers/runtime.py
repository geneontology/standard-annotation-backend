"""Construct the job runners and services used by one Celery task or CLI run."""

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
from standard_annotation_backend.services.annotation_scan import AnnotationScanRunner
from standard_annotation_backend.services.authorization_refresh_service import (
    AuthorizationRefreshService,
)
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_executors import JobExecutors
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshService,
)
from standard_annotation_backend.services.qc_report_service import QcReportScan
from standard_annotation_backend.services.rules_refresh_service import (
    RulesRefreshService,
)
from standard_annotation_backend.services.term_replacement_service import (
    TermReplacementScan,
)


def create_job_executors(
    *,
    engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    sources: RefreshSources,
    fetchers: SourceFetcher,
    dispatch_follow_up: Callable[[Job], None] | None = None,
) -> JobExecutors:
    """Build the runners for every job type and the registry that picks one.

    Args:
        engine: Engine for advisory locks held on dedicated connections.
        unit_of_work_factory: Opens transactions for services.
        sources: Configured sources for every kind.
        fetchers: Fetches sources.
        dispatch_follow_up: Receives each job that a refresh queued, such as a
            term replacement job, after the refresh commits. Celery tasks send
            the job to a worker; the CLI collects the jobs and runs them
            afterward. `None` leaves the jobs queued.
    """
    try_lock = bind_try_lock(engine)
    ontologies = sources.ontology_catalog()
    jobs = JobService(unit_of_work_factory)
    return JobExecutors(
        jobs,
        (
            RefreshRunner(
                try_lock=try_lock,
                jobs=jobs,
                unit_of_work_factory=unit_of_work_factory,
                fetchers=fetchers,
                sources=sources,
                kinds=(
                    AuthorizationRefreshService(),
                    RulesRefreshService(ontologies),
                    EntityRefreshService(),
                    OntologyRefreshService(sources),
                    AnnotationRefreshService(sources),
                ),
                dispatch_follow_up=dispatch_follow_up,
            ),
            AnnotationScanRunner(
                try_lock=try_lock,
                jobs=jobs,
                unit_of_work_factory=unit_of_work_factory,
                scans=(
                    TermReplacementScan(unit_of_work_factory, ontologies),
                    QcReportScan(unit_of_work_factory, ontologies),
                ),
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Group the settings and services used by one Celery task or CLI run."""

    settings: Settings
    unit_of_work_factory: UnitOfWorkFactory
    jobs: JobService
    executors: JobExecutors


@contextmanager
def worker_runtime(
    *,
    dispatch_follow_up: Callable[[Job], None] | None = None,
) -> Iterator[WorkerRuntime]:
    """Create a worker runtime and dispose its database engine afterward.

    Args:
        dispatch_follow_up: Receives each job that a refresh queued. See
            `create_job_executors`.
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
            yield WorkerRuntime(
                settings=settings,
                unit_of_work_factory=unit_of_work_factory,
                jobs=JobService(unit_of_work_factory),
                executors=create_job_executors(
                    engine=engine,
                    unit_of_work_factory=unit_of_work_factory,
                    sources=settings.sources,
                    fetchers=fetchers,
                    dispatch_follow_up=dispatch_follow_up,
                ),
            )
    finally:
        engine.dispose()
