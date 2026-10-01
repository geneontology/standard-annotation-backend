"""Create and run authorization synchronization jobs with Celery.

Celery Beat publishes `schedule_authorization_sync` because each scheduled
run needs a new job identifier from PostgreSQL. The scheduling task commits the
queued job before publishing `run_authorization_sync` with only that identifier.
The execution task loads its parameters and stores every state change in
PostgreSQL. Redis carries the Celery messages but does not store job state.

Using separate tasks records dispatch failures separately from execution
failures and allows other callers to execute an existing job. If Celery
redelivers an execution message, it uses the same job identifier.
"""

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from celery import Task
from celery.exceptions import Reject, Retry

from standard_annotation_backend.auth.authorization_source import (
    AuthorizationSourceClient,
)
from standard_annotation_backend.domain.entities import (
    EntityCandidateConflictError,
    EntityCatalog,
    EntityCatalogCollisionError,
    EntityImportFailureCode,
    UnknownEntitySourceError,
)
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.ontology import (
    OntologyKey,
    OntologyLoadResult,
    OntologyParseError,
)
from standard_annotation_backend.entity_sources.http import (
    EntitySourceDocument,
    EntitySourceError,
)
from standard_annotation_backend.gpi.parser import GpiParseError, parse_gpi
from standard_annotation_backend.ontology.obo_parser import parse_obo
from standard_annotation_backend.ontology.sources import OntologySourceError
from standard_annotation_backend.persistence.locks import (
    job_execution_lock,
    ontology_load_lock,
)
from standard_annotation_backend.services.entity_import_run_service import (
    EntityImportRunService,
)
from standard_annotation_backend.services.entity_import_service import (
    EntityImportService,
)
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.ontology_load_service import (
    OntologyCandidateConflictError,
)
from standard_annotation_backend.workers.celery_app import celery_app
from standard_annotation_backend.workers.runtime import worker_runtime

logger = logging.getLogger(__name__)

_SYNC_FAILURE = "Authorization synchronization failed"
_DISPATCH_FAILURE = "Authorization synchronization could not be dispatched"
_TASK_UNAVAILABLE = "Worker task could not persist state"
_ONTOLOGY_FAILURE = "Ontology load failed"
_ONTOLOGY_DISPATCH_FAILURE = "Ontology load could not be dispatched"
_ENTITY_IMPORT_FAILURE = "Entity import failed"

SOURCE_KEY = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


class OntologyLoadBusyError(RuntimeError):
    """Report that another worker is already loading the ontology."""


class WorkerTaskUnavailableError(RuntimeError):
    """Request a Celery retry without including infrastructure error details."""

    def __init__(self) -> None:
        super().__init__(_TASK_UNAVAILABLE)


def _entity_job_source_key(parameters: dict[str, object]) -> str:
    """Return the `source_key` from an entity job's stored parameters.

    Raises:
        ValueError: If the parameters are anything other than a valid `source_key`.
    """
    source_key = parameters.get("source_key")
    if (
        set(parameters) != {"source_key"}
        or not isinstance(source_key, str)
        or SOURCE_KEY.fullmatch(source_key) is None
    ):
        raise ValueError("invalid persisted entity job parameters")
    return source_key


def _parse_entity_source(
    source_format: str, document: EntitySourceDocument
) -> EntityCatalog:
    """Parse retrieved text with the parser for the source's format."""
    parsers: dict[str, Callable[[EntitySourceDocument], EntityCatalog]] = {
        "gpi": parse_gpi
    }
    return parsers[source_format](document)


def _entity_import_progress(phase: str, catalog: EntityCatalog) -> dict[str, object]:
    """Build a job's progress for one phase from catalog counts only."""
    return {
        "phase": phase,
        "source_record_count": len(catalog.records),
        "active_identifier_count": len(
            {record.entity.db_object_id for record in catalog.records}
        ),
        "warning_count": len(catalog.warnings),
    }


def _finalize_entity_import_job(
    jobs: JobService, job_id: UUID, result: dict[str, object]
) -> None:
    """Record a job's final progress and result, and mark it succeeded."""
    if result.get("unchanged") is True:
        jobs.update_progress(
            job_id, progress={"phase": "completed", "unchanged": True}, warnings=()
        )
        jobs.succeed(job_id, result=result)
        return
    warnings = result.get("warnings")
    removal_impacts = result.get("removal_impacts")
    if not isinstance(warnings, list) or not all(
        isinstance(warning, str) for warning in warnings
    ):
        raise ValueError("invalid durable entity import warnings")
    if not isinstance(removal_impacts, list):
        raise ValueError("invalid durable entity import removal impacts")
    jobs.update_progress(
        job_id,
        progress={
            "phase": "completed",
            "source_record_count": result.get("source_record_count", 0),
            "active_identifier_count": result.get("active_identifier_count", 0),
            "added_count": result.get("added_count", 0),
            "retained_count": result.get("retained_count", 0),
            "removed_count": result.get("removed_count", 0),
            "warning_count": len(warnings),
            "removal_impact_count": len(removal_impacts),
        },
        warnings=tuple(warnings),
    )
    jobs.succeed(job_id, result=result)


def _fail_entity_import_job(
    imports: EntityImportService,
    job_id: UUID,
    error: Exception,
) -> None:
    """Mark a job failed with the failure code for `error`, and delete its staging."""
    if isinstance(error, (EntitySourceError, GpiParseError)):
        try:
            failure_code = EntityImportFailureCode(error.code)
        except ValueError:
            failure_code = (
                EntityImportFailureCode.SOURCE_ERROR
                if isinstance(error, EntitySourceError)
                else EntityImportFailureCode.ROW_VALIDATION
            )
    elif isinstance(error, UnknownEntitySourceError):
        failure_code = EntityImportFailureCode.UNKNOWN_SOURCE
    elif isinstance(error, EntityCatalogCollisionError):
        failure_code = EntityImportFailureCode.CATALOG_COLLISION
    elif isinstance(error, EntityCandidateConflictError):
        failure_code = EntityImportFailureCode.CANDIDATE_CONFLICT
    else:
        failure_code = EntityImportFailureCode.INVALID_PARAMETERS
    logger.error(
        "Entity import job failed: job_id=%s failure_type=%s failure_code=%s",
        job_id,
        type(error).__name__,
        failure_code.value,
    )
    imports.fail(job_id, error=_ENTITY_IMPORT_FAILURE, failure_code=failure_code.value)


@celery_app.task(bind=True, max_retries=None, name="sab.entity_import.run")
def run_entity_import(task: Task, job_id: str) -> None:
    """Run one entity import job for a configured source.

    The source is fetched and parsed with the parser for its configured format.
    Everything after parsing uses the format-independent entity catalog.

    Celery can deliver the same task more than once, for example after a worker
    stops. Each run therefore checks the database before doing new work, in
    this order:

    1. If the job has already finished, do nothing.
    2. If this job already published its catalog, copy the stored result into
       the job without fetching the source.
    3. If this job has committed staging, check it and publish it without
       fetching the source.

    Only then is the source fetched. If the URL and checksum match the active
    catalog, the job succeeds without publishing. Source, parsing, and catalog
    failures mark the job failed with a `failure_code`. Other errors, such as a
    lost database connection, make Celery retry the task, keeping the job and
    any staging for the next attempt.
    """
    failure_type: str | None = None
    try:
        durable_job_id = UUID(job_id)
        with (
            worker_runtime() as runtime,
            job_execution_lock(runtime.engine, durable_job_id) as acquired,
        ):
            if not acquired:
                return
            job = runtime.jobs.start(durable_job_id)
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                return
            try:
                if job.job_type is not JobType.ENTITY_IMPORT:
                    raise ValueError("unexpected job type")
                source_key = _entity_job_source_key(job.parameters)
            except (KeyError, TypeError, ValueError) as error:
                _fail_entity_import_job(runtime.entity_imports, durable_job_id, error)
                return
            try:
                completed = runtime.entity_imports.completed(durable_job_id)
                if completed is not None:
                    _finalize_entity_import_job(runtime.jobs, durable_job_id, completed)
                    return
                resumed = runtime.entity_imports.resume(
                    job_id=durable_job_id, actor_id=job.requested_by
                )
                if resumed is not None:
                    _finalize_entity_import_job(
                        runtime.jobs, durable_job_id, resumed.to_job_result()
                    )
                    return
                source = runtime.entity_sources.source(source_key)
                runtime.jobs.update_progress(
                    durable_job_id, progress={"phase": "fetching"}
                )
                document = runtime.entity_source_client.fetch(source)
                unchanged = runtime.entity_imports.unchanged(
                    source_key=source_key,
                    source_url=document.source_url,
                    source_checksum=document.source_checksum,
                )
                if unchanged is not None:
                    _finalize_entity_import_job(
                        runtime.jobs, durable_job_id, unchanged.to_job_result()
                    )
                    return
                runtime.jobs.update_progress(
                    durable_job_id, progress={"phase": "parsing"}
                )
                catalog = _parse_entity_source(source.format, document)
                runtime.jobs.update_progress(
                    durable_job_id,
                    progress=_entity_import_progress("staging", catalog),
                    warnings=catalog.warnings,
                )
                runtime.entity_imports.stage(
                    job_id=durable_job_id, source_key=source_key, catalog=catalog
                )
                runtime.jobs.update_progress(
                    durable_job_id,
                    progress=_entity_import_progress("publishing", catalog),
                    warnings=catalog.warnings,
                )
                result = runtime.entity_imports.publish(
                    job_id=durable_job_id, actor_id=job.requested_by
                )
            except (
                EntityCandidateConflictError,
                EntityCatalogCollisionError,
                EntitySourceError,
                GpiParseError,
                UnknownEntitySourceError,
            ) as error:
                _fail_entity_import_job(runtime.entity_imports, durable_job_id, error)
                return
            _finalize_entity_import_job(
                runtime.jobs, durable_job_id, result.to_job_result()
            )
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, job_id, failure_type)


@celery_app.task(bind=True, max_retries=None, name="sab.entity_catalog_retirement.run")
def run_entity_catalog_retirement(task: Task, job_id: str) -> None:
    """Retire the active catalog of a source that is no longer configured.

    The retirement is committed with its audit event. If the worker stops before
    marking the job succeeded, the next run of the task returns the stored
    result.
    """
    failure_type: str | None = None
    try:
        durable_job_id = UUID(job_id)
        with (
            worker_runtime() as runtime,
            job_execution_lock(runtime.engine, durable_job_id) as acquired,
        ):
            if not acquired:
                return
            job = runtime.jobs.start(durable_job_id)
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                return
            try:
                if job.job_type is not JobType.ENTITY_CATALOG_RETIREMENT:
                    raise ValueError("unexpected job type")
                source_key = _entity_job_source_key(job.parameters)
            except (KeyError, TypeError, ValueError) as error:
                _fail_entity_import_job(runtime.entity_imports, durable_job_id, error)
                return
            result = runtime.entity_imports.retire(
                job_id=durable_job_id,
                source_key=source_key,
                configured=source_key in runtime.entity_sources.keys,
                actor_id=job.requested_by,
            )
            runtime.jobs.update_progress(
                durable_job_id,
                progress={
                    "phase": "completed",
                    "retired": result.retired,
                    "removed_count": len(result.removal_impacts),
                },
            )
            runtime.jobs.succeed(durable_job_id, result=result.to_job_result())
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, job_id, failure_type)


@celery_app.task(bind=True, max_retries=None, name="sab.ontology_load.prune")
def prune_ontology_snapshots(task: Task, ontology_key: str) -> None:
    """Delete unneeded term and closure rows for one configured ontology.

    The task uses the same per-ontology lock as ontology loads and requests a retry
    when the lock or required infrastructure is unavailable.

    Args:
        task: Bound Celery task used to request retries.
        ontology_key: Configured ontology key to clean up.
    """
    try:
        key = OntologyKey(ontology_key)
    except ValueError:
        logger.error(
            "Ontology pruning ignored unknown key: ontology_key=%s", ontology_key
        )
        return

    failure_type: str | None = None
    try:
        with (
            worker_runtime() as runtime,
            ontology_load_lock(runtime.engine, key) as acquired,
        ):
            if not acquired:
                raise OntologyLoadBusyError
            runtime.ontology_loads[key].prune(pruned_at=datetime.now(UTC))
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, f"ontology:{ontology_key}", failure_type)


@celery_app.task(bind=True, max_retries=None, name="sab.ontology_load.run")
def run_ontology_load(task: Task, job_id: str) -> None:
    """Run one ontology-load job from retrieval through activation."""
    failure_type: str | None = None
    try:
        durable_job_id = UUID(job_id)
        with (
            worker_runtime() as runtime,
            job_execution_lock(runtime.engine, durable_job_id) as job_acquired,
        ):
            if not job_acquired:
                return
            job = runtime.jobs.start(durable_job_id)
            try:
                if job.job_type is not JobType.ONTOLOGY_LOAD:
                    raise ValueError("unexpected job type")
                ontology_key = OntologyKey(job.parameters["ontology"])
                load_service = runtime.ontology_loads[ontology_key]
            except (KeyError, TypeError, ValueError):
                if job.status not in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                    runtime.jobs.fail(durable_job_id, error=_ONTOLOGY_FAILURE)
                return
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                prune_ontology_snapshots.delay(ontology_key.value)
                return
            completed = load_service.completed(durable_job_id)
            if completed is not None:
                _finalize_ontology_job(runtime.jobs, durable_job_id, completed)
                prune_ontology_snapshots.delay(ontology_key.value)
                return
            definition = runtime.ontology_registry.definition(ontology_key)
            with ontology_load_lock(runtime.engine, ontology_key) as ontology_acquired:
                if not ontology_acquired:
                    raise OntologyLoadBusyError
                try:
                    document = runtime.ontology_registry.source(ontology_key).fetch()
                    if load_service.matches_active(document):
                        result = OntologyLoadResult.unchanged(document)
                    else:
                        snapshot = parse_obo(document, definition)
                        load_service.stage(
                            job_id=durable_job_id,
                            document=document,
                            snapshot=snapshot,
                        )
                        result = load_service.activate(
                            job_id=durable_job_id,
                            actor_id=job.requested_by,
                        )
                except (
                    OntologyCandidateConflictError,
                    OntologyParseError,
                    OntologySourceError,
                ) as error:
                    logger.error(
                        "Ontology load job failed: job_id=%s failure_type=%s",
                        durable_job_id,
                        type(error).__name__,
                    )
                    runtime.jobs.fail(durable_job_id, error=_ONTOLOGY_FAILURE)
                    prune_ontology_snapshots.delay(ontology_key.value)
                    return
            _finalize_ontology_job(runtime.jobs, durable_job_id, result.to_job_result())
            prune_ontology_snapshots.delay(ontology_key.value)
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, job_id, failure_type)


def _finalize_ontology_job(
    jobs: JobService, job_id: UUID, result: dict[str, object]
) -> None:
    """Mark an ontology-load job successful using its stored activation result."""
    findings = result.get("findings")
    finding_count = len(findings) if isinstance(findings, list) else 0
    ontology_warnings = result.get("ontology_warnings")
    ontology_warning_count = (
        len(ontology_warnings) if isinstance(ontology_warnings, list) else 0
    )
    progress = {
        "phase": "completed",
        "annotation_scan_count": result.get("annotation_scan_count", 0),
        "annotation_update_count": result.get("annotation_update_count", 0),
        "annotation_skip_count": result.get("annotation_skip_count", 0),
        "finding_count": finding_count,
        "ontology_warning_count": ontology_warning_count,
    }
    warnings: list[str] = []
    if ontology_warning_count:
        noun = "warning" if ontology_warning_count == 1 else "warnings"
        warnings.append(
            f"Ontology load completed with {ontology_warning_count} ontology {noun}"
        )
    if finding_count:
        warnings.append(f"Ontology load completed with {finding_count} findings")
    jobs.update_progress(job_id, progress=progress, warnings=tuple(warnings))
    jobs.succeed(job_id, result=result)


def _source_client(parameters: dict[str, object]) -> AuthorizationSourceClient:
    """Build a source client from parameters stored on a job."""
    repository = parameters.get("source_repository")
    ref = parameters.get("source_ref")
    path = parameters.get("source_path")
    if (
        not isinstance(repository, str)
        or not isinstance(ref, str)
        or not isinstance(path, str)
    ):
        raise ValueError("invalid persisted authorization source")
    return AuthorizationSourceClient(repository=repository, ref=ref, path=path)


def _retry_safely(task: Task, job_id: str, failure_type: str) -> None:
    """Request a retry while logging only the job ID and exception type."""
    logger.error(
        "Worker task unavailable: job_id=%s failure_type=%s",
        job_id,
        failure_type,
    )
    try:
        task.retry(
            exc=WorkerTaskUnavailableError(),
            countdown=30,
        )
    except Retry as retry:
        raise retry from None
    except WorkerTaskUnavailableError as error:
        raise error from None
    except Exception:
        raise Reject(WorkerTaskUnavailableError(), requeue=True) from None


@celery_app.task(bind=True, max_retries=None, name="sab.authorization_sync.run")
def run_authorization_sync(task: Task, job_id: str) -> None:
    """Run the authorization synchronization stored for one job.

    The Celery message contains only the job identifier. The task reads its
    parameters from PostgreSQL and writes progress and results there. If Celery
    redelivers the message, the job lock prevents overlapping executions and a
    completed job makes the later delivery do nothing.

    If the task cannot read or update job state, it asks Celery to retry. If the
    authorization synchronization fails, it records the configured public error
    and marks the job failed.
    """
    failure_type: str | None = None
    try:
        durable_job_id = UUID(job_id)
        with (
            worker_runtime() as runtime,
            job_execution_lock(runtime.engine, durable_job_id) as acquired,
        ):
            if not acquired:
                return
            job = runtime.jobs.start(durable_job_id)
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                return
            try:
                if job.job_type is not JobType.AUTHORIZATION_SYNC:
                    raise ValueError("unexpected job type")
                source_client = _source_client(job.parameters)
                persisted_sha = job.parameters.get("source_commit_sha")
                if persisted_sha is None:
                    source = source_client.fetch()
                    job = runtime.jobs.update_parameters(
                        job.job_id,
                        parameters={
                            **job.parameters,
                            "source_commit_sha": source.source_commit_sha,
                        },
                    )
                elif isinstance(persisted_sha, str):
                    source = source_client.fetch_at_commit(persisted_sha)
                else:
                    raise ValueError("invalid persisted source commit")
                sync = runtime.authorization_sync.synchronize(
                    source.yaml_text,
                    source.source_repository,
                    source.source_commit_sha,
                    actor_id=job.requested_by,
                    job_id=job.job_id,
                )
                runtime.jobs.succeed(
                    job.job_id,
                    result={
                        "sync_id": str(sync.sync_id),
                        "source_repository": sync.source_repository,
                        "source_commit_sha": sync.source_commit_sha,
                        "user_count": sync.user_count,
                        "group_count": sync.group_count,
                        "assignment_count": sync.assignment_count,
                        "applied": sync.applied,
                    },
                )
            except Exception as error:
                logger.error(
                    "Authorization synchronization job failed: job_id=%s failure_type=%s",
                    durable_job_id,
                    type(error).__name__,
                )
                runtime.jobs.fail(durable_job_id, error=_SYNC_FAILURE)
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, job_id, failure_type)


@celery_app.task(bind=True, max_retries=None, name="sab.authorization_sync.schedule")
def schedule_authorization_sync(task: Task) -> None:
    """Create a scheduled job before publishing its execution task.

    Celery Beat invokes this task, which does not perform the synchronization.
    It commits a queued job with the configured source parameters and then
    publishes `run_authorization_sync` with the job identifier. If publication
    raises an exception, it marks the queued job failed.

    A Beat run does not have its own stored identifier. If the worker stops
    during job creation or publication, Celery redelivery may create another job
    for the same scheduled run.
    """
    failure_type: str | None = None
    try:
        with worker_runtime() as runtime:
            job = runtime.jobs.create(
                job_type=JobType.AUTHORIZATION_SYNC,
                requested_by="scheduler",
                parameters={
                    "source_repository": runtime.settings.authorization_source_repository,
                    "source_ref": runtime.settings.authorization_source_ref,
                    "source_path": runtime.settings.authorization_source_path,
                },
            )
            try:
                run_authorization_sync.delay(str(job.job_id))
            except Exception as error:
                logger.error(
                    "Authorization synchronization dispatch failed: job_id=%s failure_type=%s",
                    job.job_id,
                    type(error).__name__,
                )
                runtime.jobs.fail(job.job_id, error=_DISPATCH_FAILURE)
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, "scheduler", failure_type)


@celery_app.task(bind=True, max_retries=None, name="sab.ontology_load.schedule")
def schedule_ontology_load(task: Task) -> None:
    """Create and dispatch the scheduled GO ontology-load job."""
    failure_type: str | None = None
    try:
        with worker_runtime() as runtime:
            job = runtime.jobs.create(
                job_type=JobType.ONTOLOGY_LOAD,
                requested_by="scheduler",
                parameters={"ontology": OntologyKey.GO.value},
            )
            try:
                run_ontology_load.delay(str(job.job_id))
            except Exception as error:
                logger.error(
                    "Ontology load dispatch failed: job_id=%s failure_type=%s",
                    job.job_id,
                    type(error).__name__,
                )
                runtime.jobs.fail(job.job_id, error=_ONTOLOGY_DISPATCH_FAILURE)
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, "scheduler", failure_type)


def dispatch_entity_job(job: Job) -> None:
    """Send one entity job to the Celery task that runs its job type."""
    task = (
        run_entity_catalog_retirement
        if job.job_type is JobType.ENTITY_CATALOG_RETIREMENT
        else run_entity_import
    )
    task.delay(str(job.job_id))


@celery_app.task(bind=True, max_retries=None, name="sab.entity_import.schedule")
def schedule_entity_imports(task: Task) -> None:
    """Start the scheduled import of every configured entity source.

    Celery Beat runs this task on `SAB_ENTITY_IMPORT_CRON`. If it is retried after
    its jobs were committed, those jobs are reused and sent again instead of new
    ones being created.
    """
    failure_type: str | None = None
    try:
        with worker_runtime() as runtime:
            EntityImportRunService(
                runtime.unit_of_work_factory,
                runtime.entity_sources,
                dispatch_entity_job,
            ).start(requested_by="scheduler", source_key=None)
    except Exception as error:
        failure_type = type(error).__name__
    if failure_type is not None:
        _retry_safely(task, "scheduler", failure_type)
