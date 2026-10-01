"""Verify retirement of entity catalogs whose source is no longer configured."""

from collections.abc import Callable
from uuid import UUID

import pytest
from refresh_helpers import FakeFetchers, build_runner, sources_with_entities
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from test_entity_refresh_service import stage

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AuditEventRecord,
    EntityCatalogSnapshotRecord,
    EntityMembershipRecord,
    EntitySourceRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh import runner as refresh_runner
from standard_annotation_backend.refresh.runner import RefreshRunner
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import (
    Job,
    JobService,
    job_from_record,
)


@pytest.fixture
def services(
    unit_of_work_factory: UnitOfWorkFactory,
) -> tuple[JobService, EntityRefreshService]:
    return JobService(unit_of_work_factory), EntityRefreshService(unit_of_work_factory)


NO_ENTITIES = sources_with_entities({})
ONLY_MGI = sources_with_entities(
    {"mgi": {"type": "https", "url": "https://example.org/mgi.gpi"}}
)
ONLY_RGD = sources_with_entities(
    {"rgd": {"type": "https", "url": "https://example.org/rgd.gpi"}}
)


def _runner(
    engine: Engine, unit_of_work_factory: UnitOfWorkFactory, sources: RefreshSources
) -> RefreshRunner:
    """Build a runner whose fetchers fail the test if a source is fetched."""
    return build_runner(engine, unit_of_work_factory, FakeFetchers({}), sources=sources)


def _publish(
    imports: EntityRefreshService,
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
        job_type=JobType.ENTITY_RETIREMENT,
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
                    AuditEventRecord.action == AuditAction.ENTITY_RETIRED
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
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    services: tuple[JobService, EntityRefreshService],
    seed_annotation: Callable[[str], UUID],
) -> None:
    """Retiring a source removes its entities and reports affected annotations."""
    jobs, imports = services
    _publish(imports, session_factory, "mgi", "MGI:1", "MGI:2")
    _publish(imports, session_factory, "rgd", "RGD:1")
    annotation_id = seed_annotation("MGI:1")  # active annotation on a retired entity
    job_id = _retirement_job(jobs, "mgi")

    _runner(database_engine, unit_of_work_factory, ONLY_RGD).run_retirement(job_id)

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
                    AuditEventRecord.action == AuditAction.ENTITY_RETIRED
                )
            )
        )
        assert len(audits) == 1
    with jobs._unit_of_work_factory() as uow:
        record = uow.jobs.get(job_id)
        assert record is not None
        assert record.status == JobStatus.SUCCEEDED.value
        assert record.progress == {
            "phase": "completed",
            "retired": True,
            "removed_count": 2,
        }
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
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    services: tuple[JobService, EntityRefreshService],
) -> None:
    """A retirement job run again after a lost success update finishes the job.

    The job succeeds with the result of the committed retirement, and the
    retirement is audited only once.
    """
    jobs, imports = services
    _publish(imports, session_factory, "mgi", "MGI:1")
    runner = _runner(database_engine, unit_of_work_factory, NO_ENTITIES)
    job_id = _retirement_job(jobs, "mgi")
    original_succeed = JobService.succeed
    failures = 1

    def fail_once(
        service: JobService,
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
            service, received_job_id, result=result, artifact_uri=artifact_uri
        )

    monkeypatch.setattr(JobService, "succeed", fail_once)

    with pytest.raises(RuntimeError, match="connection dropped"):
        runner.run_retirement(job_id)

    assert _job_record(jobs, job_id).status == JobStatus.RUNNING

    runner.run_retirement(job_id)

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
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    services: tuple[JobService, EntityRefreshService],
    case: str,
) -> None:
    """Nothing is retired when the source is configured again or has no catalog.

    The job succeeds with a result that reports no retirement, and the active
    catalog is unchanged.
    """
    jobs, imports = services
    if case == "configured":
        _publish(imports, session_factory, "mgi", "MGI:1")
        sources = ONLY_MGI
    else:
        sources = NO_ENTITIES
    active_before = _active_ids(session_factory)
    job_id = _retirement_job(jobs, "mgi")

    _runner(database_engine, unit_of_work_factory, sources).run_retirement(job_id)

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
        {"source_key": "Bad Key"},
        {"source_key": 7},
        {"source_key": "mgi", "source_url": "https://example.org/x"},
    ],
)
@pytest.mark.parametrize(
    ("job_type", "error"),
    [
        (JobType.ENTITY_REFRESH, "Entity refresh failed"),
        (JobType.ENTITY_RETIREMENT, "Entity refresh failed"),
    ],
    ids=["refresh", "retirement"],
)
def test_invalid_entity_job_parameters_fail_terminally(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    services: tuple[JobService, EntityRefreshService],
    job_type: JobType,
    error: str,
    parameters: dict[str, object],
) -> None:
    """Entity jobs with malformed stored parameters fail with `invalid_parameters`.

    The job gets a generic error message, and the logs omit the parameter values.
    """
    log_records: list[str] = []

    def record_log(message: str, *args: object) -> None:
        log_records.append(message % args)

    monkeypatch.setattr(refresh_runner.logger, "error", record_log)
    jobs, _imports = services
    runner = _runner(database_engine, unit_of_work_factory, ONLY_MGI)
    job_id = jobs.create(
        job_type=job_type,
        requested_by="scheduler",
        parameters=parameters,
    ).job_id

    if job_type is JobType.ENTITY_RETIREMENT:
        runner.run_retirement(job_id)
    else:
        runner.run(job_id)

    record = _job_record(jobs, job_id)
    assert record.status == JobStatus.FAILED
    assert record.error == error
    assert record.progress == {
        "phase": "failed",
        "failure_code": "invalid_parameters",
    }
    logs = "\n".join(log_records)
    assert str(job_id) in logs
    assert "failure_code=invalid_parameters" in logs
    assert "MGI" not in logs
    assert "Bad Key" not in logs
    assert "example.org" not in logs
