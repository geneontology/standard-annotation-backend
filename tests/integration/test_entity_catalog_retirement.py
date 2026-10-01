"""Verify retirement of entity catalogs whose source is no longer configured."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from celery import Task
from celery.exceptions import Retry
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from test_entity_import_service import stage

from standard_annotation_backend.config import GpiEntitySourceSettings
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.entity_sources.registry import EntitySourceRegistry
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AuditEventRecord,
    EntityCatalogSnapshotRecord,
    EntityMembershipRecord,
    EntitySourceRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.entity_import_service import (
    EntityImportService,
)
from standard_annotation_backend.services.job_service import (
    Job,
    JobService,
    job_from_record,
)
from standard_annotation_backend.workers import tasks


@pytest.fixture
def services(
    unit_of_work_factory: UnitOfWorkFactory,
) -> tuple[JobService, EntityImportService]:
    return JobService(unit_of_work_factory), EntityImportService(unit_of_work_factory)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    jobs: JobService,
    imports: EntityImportService,
    registry: EntitySourceRegistry,
) -> None:
    @contextmanager
    def runtime() -> Iterator[object]:
        yield SimpleNamespace(
            engine=engine,
            jobs=jobs,
            entity_imports=imports,
            entity_sources=registry,
            entity_source_client=None,
        )

    monkeypatch.setattr(tasks, "worker_runtime", runtime)


def _publish(
    imports: EntityImportService,
    session_factory: sessionmaker[Session],
    source: str,
    *identifiers: str,
) -> None:
    imports.publish(
        job_id=stage(imports, session_factory, *identifiers, source=source),
        actor_id="curator",
    )


def _retirement_job(jobs: JobService, source: str) -> UUID:
    return jobs.create(
        job_type=JobType.ENTITY_CATALOG_RETIREMENT,
        requested_by="scheduler",
        parameters={"source_key": source},
    ).job_id


def _job_record(jobs: JobService, job_id: UUID) -> Job:
    """Read a job's stored state."""
    with jobs._unit_of_work_factory() as uow:
        record = uow.jobs.get(job_id)
        assert record is not None
        return job_from_record(record)


def _retirement_audits(
    session_factory: sessionmaker[Session],
) -> list[AuditEventRecord]:
    """Return every catalog retirement audit event."""
    with session_factory() as session:
        return list(
            session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.action == AuditAction.ENTITY_CATALOG_RETIRED
                )
            )
        )


def _active_ids(session_factory: sessionmaker[Session]) -> list[str]:
    """Return every active entity identifier in sorted order."""
    with session_factory() as session:
        return list(
            session.scalars(
                select(EntityMembershipRecord.db_object_id).order_by(
                    EntityMembershipRecord.db_object_id
                )
            )
        )


def test_retirement_removes_membership_and_reports_impacts(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    services: tuple[JobService, EntityImportService],
    seed_annotation: Callable[[str], UUID],
) -> None:
    """Retiring a source removes its entities and reports affected annotations."""
    jobs, imports = services
    _publish(imports, session_factory, "mgi", "MGI:1", "MGI:2")
    _publish(imports, session_factory, "rgd", "RGD:1")
    annotation_id = seed_annotation("MGI:1")  # active annotation on a retired entity
    _install(
        monkeypatch,
        database_engine,
        jobs,
        imports,
        EntitySourceRegistry(
            {"rgd": GpiEntitySourceSettings(url="https://example.org/rgd.gpi")}
        ),
    )
    job_id = _retirement_job(jobs, "mgi")

    tasks.run_entity_catalog_retirement.run(str(job_id))

    with session_factory() as session:
        assert list(
            session.scalars(
                select(EntityMembershipRecord.db_object_id).order_by(
                    EntityMembershipRecord.db_object_id
                )
            )
        ) == ["RGD:1"]
        assert session.scalar(select(func.count()).select_from(EntitySourceRecord)) == 1
        retired = session.scalar(
            select(EntityCatalogSnapshotRecord).where(
                EntityCatalogSnapshotRecord.source_key == "mgi"
            )
        )
        assert retired is not None
        assert retired.active is False
        assert retired.retired_by_job_id == job_id
        assert retired.retired_at is not None
        assert session.get(AnnotationRecord, annotation_id) is not None
        audits = list(
            session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.action == AuditAction.ENTITY_CATALOG_RETIRED
                )
            )
        )
        assert len(audits) == 1
    with jobs._unit_of_work_factory() as uow:
        record = uow.jobs.get(job_id)
        assert record is not None
        assert record.status == JobStatus.SUCCEEDED.value
        assert record.result == {
            "source_key": "mgi",
            "retired": True,
            "snapshot_id": str(retired.snapshot_id),
            "removed_count": 2,
            "removal_impacts": [
                {"db_object_id": "MGI:1", "annotation_ids": [str(annotation_id)]},
                {"db_object_id": "MGI:2", "annotation_ids": []},
            ],
        }


def test_redelivered_retirement_recovers_result_without_second_audit(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    services: tuple[JobService, EntityImportService],
) -> None:
    """A retirement task run again after a lost success update finishes the job.

    The job succeeds with the result of the committed retirement, and the
    retirement is audited only once.
    """
    jobs, imports = services
    _publish(imports, session_factory, "mgi", "MGI:1")
    _install(monkeypatch, database_engine, jobs, imports, EntitySourceRegistry({}))
    job_id = _retirement_job(jobs, "mgi")
    original_succeed = jobs.succeed
    failures = 1

    def fail_once(
        received_job_id: UUID,
        *,
        result: dict[str, object],
        artifact_uri: str | None = None,
    ) -> Job:
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("connection dropped after retirement")
        return original_succeed(
            received_job_id, result=result, artifact_uri=artifact_uri
        )

    def retry(*, exc: Exception, countdown: int) -> None:
        assert countdown == 30
        raise Retry(exc=exc)

    monkeypatch.setattr(jobs, "succeed", fail_once)
    monkeypatch.setattr(tasks.run_entity_catalog_retirement, "retry", retry)

    with pytest.raises(Retry):
        tasks.run_entity_catalog_retirement.run(str(job_id))

    assert _job_record(jobs, job_id).status == JobStatus.RUNNING

    tasks.run_entity_catalog_retirement.run(str(job_id))

    audits = _retirement_audits(session_factory)
    assert len(audits) == 1
    record = _job_record(jobs, job_id)
    assert record.status == JobStatus.SUCCEEDED
    assert record.result == {
        "source_key": "mgi",
        "retired": True,
        "snapshot_id": audits[0].details["snapshot_id"],
        "removed_count": 1,
        "removal_impacts": [{"db_object_id": "MGI:1", "annotation_ids": []}],
    }
    assert _active_ids(session_factory) == []


@pytest.mark.parametrize("case", ["configured", "no_catalog"])
def test_retirement_without_active_catalog_or_of_configured_source_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    services: tuple[JobService, EntityImportService],
    case: str,
) -> None:
    """Nothing is retired when the source is configured again or has no catalog.

    The job succeeds with a result that reports no retirement, and the active
    catalog is unchanged.
    """
    jobs, imports = services
    if case == "configured":
        _publish(imports, session_factory, "mgi", "MGI:1")
        registry = EntitySourceRegistry(
            {"mgi": GpiEntitySourceSettings(url="https://example.org/mgi.gpi")}
        )
    else:
        registry = EntitySourceRegistry({})
    active_before = _active_ids(session_factory)
    _install(monkeypatch, database_engine, jobs, imports, registry)
    job_id = _retirement_job(jobs, "mgi")

    tasks.run_entity_catalog_retirement.run(str(job_id))

    record = _job_record(jobs, job_id)
    assert record.status == JobStatus.SUCCEEDED
    assert record.result == {"source_key": "mgi", "retired": False}
    assert _retirement_audits(session_factory) == []
    assert _active_ids(session_factory) == active_before


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"source_key": "MGI"},
        {"source_key": 7},
        {"source_key": "mgi", "source_url": "https://example.org/x"},
    ],
)
@pytest.mark.parametrize(
    ("task", "job_type"),
    [
        (tasks.run_entity_import, JobType.ENTITY_IMPORT),
        (tasks.run_entity_catalog_retirement, JobType.ENTITY_CATALOG_RETIREMENT),
    ],
    ids=["import", "retirement"],
)
def test_invalid_entity_job_parameters_fail_terminally(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    services: tuple[JobService, EntityImportService],
    task: Task,
    job_type: JobType,
    parameters: dict[str, object],
) -> None:
    """Entity jobs with malformed stored parameters fail with `invalid_parameters`.

    The job gets a generic error message, and the logs omit the parameter values.
    """
    log_records: list[str] = []

    def record_log(message: str, *args: object) -> None:
        log_records.append(message % args)

    monkeypatch.setattr(tasks.logger, "error", record_log)
    jobs, imports = services
    _install(monkeypatch, database_engine, jobs, imports, EntitySourceRegistry({}))
    job_id = jobs.create(
        job_type=job_type,
        requested_by="scheduler",
        parameters=parameters,
    ).job_id

    task.run(str(job_id))

    record = _job_record(jobs, job_id)
    assert record.status == JobStatus.FAILED
    assert record.error == "Entity import failed"
    assert record.progress["failure_code"] == "invalid_parameters"
    logs = "\n".join(log_records)
    assert "failure_type=ValueError" in logs
    assert "MGI" not in logs
    assert "example.org" not in logs
