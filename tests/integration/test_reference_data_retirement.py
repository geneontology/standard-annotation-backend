"""Verify that refreshing every source retires sources the configuration dropped."""

from collections.abc import Callable
from uuid import UUID

from refresh_helpers import (
    TEST_ONTOLOGIES,
    TEST_SOURCES,
    FakeFetchers,
    build_runner,
    gpi_bytes,
)
from seeding import (
    SHIPPED_SOURCES,
    active_snapshot,
    create_job,
    seed_entity_catalog,
)
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.auth import system_context
from standard_annotation_backend.domain.jobs import JobFailureCode, JobType
from standard_annotation_backend.domain.refresh import (
    RefreshKindName,
)
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AuditEventRecord,
    ReferenceSnapshotRecord,
    StoredEntityRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_validator import (
    AnnotationValidator,
    Candidate,
)
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.refresh_start_service import (
    RefreshStart,
    RefreshStartService,
)


def _start(
    unit_of_work_factory: UnitOfWorkFactory, kind: RefreshKindName, key: str | None
) -> RefreshStart:
    return RefreshStartService(
        unit_of_work_factory, TEST_SOURCES, lambda _job: None
    ).start(kind, context=system_context("scheduler"), source_key=key)


def _audit_count(session_factory: sessionmaker[Session], action: str) -> int:
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(AuditEventRecord)
                .where(AuditEventRecord.action == action)
            )
            or 0
        )


def _stored_annotation(
    session_factory: sessionmaker[Session], annotation_id: UUID
) -> tuple[int, str, dict[str, object]]:
    """Return an annotation's current version, status, and data."""
    with session_factory() as session:
        record = session.get(AnnotationRecord, annotation_id)
        assert record is not None
        return record.current_version, record.status, record.annotation_data


def _issue_types(
    unit_of_work_factory: UnitOfWorkFactory, annotation_id: UUID
) -> list[str]:
    """Validate a stored annotation as it stands and return its issue types."""
    with unit_of_work_factory() as uow:
        record = uow.annotations.session.get(AnnotationRecord, annotation_id)
        assert record is not None
        [checked] = AnnotationValidator(TEST_ONTOLOGIES).check(
            uow, [Candidate(record.annotation_data, annotation_id)]
        )
    return [issue["type"] for issue in checked.issues]


def test_a_retired_catalogs_entities_stop_resolving_but_their_annotations_stay(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    reference_data: Callable[..., None],
    seed_annotation: Callable[[str], UUID],
) -> None:
    """Validation no longer knows a retired catalog's entity; its annotation is kept.

    The annotation keeps its version and data. Validating it now reports
    `unknown_entity`, as it would for any entity that no active catalog lists.
    """
    seed_entity_catalog(session_factory, ["ZFIN:1"], source_key="zfin")
    annotation_id = seed_annotation("ZFIN:1")
    before = _stored_annotation(session_factory, annotation_id)
    assert "unknown_entity" not in _issue_types(unit_of_work_factory, annotation_id)

    _start(unit_of_work_factory, RefreshKindName.ENTITY, None)

    assert _stored_annotation(session_factory, annotation_id) == before
    assert "unknown_entity" in _issue_types(unit_of_work_factory, annotation_id)


def test_a_retirement_is_audited_with_the_requester_and_the_retired_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """The audit event names the requester, the source, and the retired snapshot."""
    snapshot_id = seed_entity_catalog(session_factory, ["ZFIN:1"], source_key="zfin")

    _start(unit_of_work_factory, RefreshKindName.ENTITY, None)

    with session_factory() as session:
        event = session.scalars(
            select(AuditEventRecord).where(AuditEventRecord.action == "entity.retired")
        ).one()
        assert event.actor_id == "scheduler"
        assert event.job_id is None
        assert event.details == {"source_key": "zfin", "snapshot_id": str(snapshot_id)}


def test_refresh_all_ontologies_retires_ontologies_removed_from_configuration(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    reference_data: Callable[..., None],
) -> None:
    """Every stored ontology that the sources file no longer lists is retired."""
    stored = set(SHIPPED_SOURCES.keys(RefreshKindName.ONTOLOGY))
    configured = set(TEST_SOURCES.keys(RefreshKindName.ONTOLOGY))

    started = _start(unit_of_work_factory, RefreshKindName.ONTOLOGY, None)

    assert set(started.retired) == stored - configured
    assert active_snapshot(session_factory, SnapshotKind.ONTOLOGY, "chebi") is None
    assert active_snapshot(session_factory, SnapshotKind.ONTOLOGY, "go") is not None
    assert _audit_count(session_factory, "ontology.retired") == len(stored - configured)


def test_the_next_job_of_the_kind_prunes_a_retired_catalog(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A retired catalog's records are deleted by the kind's next refresh job."""
    snapshot_id = seed_entity_catalog(session_factory, ["ZFIN:1"], source_key="zfin")
    started = _start(unit_of_work_factory, RefreshKindName.ENTITY, None)
    mgi = next(job for job in started.jobs if job.parameters["source_key"] == "mgi")

    build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({"mgi": gpi_bytes("MGI:MGI:1")}),
    ).run(mgi.job_id)

    with session_factory() as session:
        retired = session.get(ReferenceSnapshotRecord, snapshot_id)
        assert retired is not None and retired.pruned_at is not None
        assert retired.retired_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(StoredEntityRecord)
                .where(StoredEntityRecord.snapshot_id == snapshot_id)
            )
            == 0
        )


def test_a_removed_source_with_an_unfinished_job_is_retired_later(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A source whose refresh job is still unfinished is retired by a later start."""
    seed_entity_catalog(session_factory, ["ZFIN:1"], source_key="zfin")
    pending: Job = create_job(
        unit_of_work_factory,
        job_type=JobType.ENTITY_REFRESH,
        parameters={"source_key": "zfin"},
    )

    first = _start(unit_of_work_factory, RefreshKindName.ENTITY, None)

    assert first.retired == ()
    assert active_snapshot(session_factory, SnapshotKind.ENTITY, "zfin") is not None

    JobService(unit_of_work_factory).fail(
        pending.job_id,
        error="Entity refresh failed",
        failure_code=JobFailureCode.UNKNOWN_SOURCE,
    )
    second = _start(unit_of_work_factory, RefreshKindName.ENTITY, None)

    assert second.retired == ("zfin",)


def test_refreshing_one_source_retires_nothing(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Only refreshing every source of a kind retires removed sources."""
    seed_entity_catalog(session_factory, ["ZFIN:1"], source_key="zfin")

    started = _start(unit_of_work_factory, RefreshKindName.ENTITY, "mgi")

    assert started.retired == ()
    assert active_snapshot(session_factory, SnapshotKind.ENTITY, "zfin") is not None
