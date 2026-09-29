"""Verify deletion and retention of stored ontology snapshot data."""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.audit import AuditAction, AuditResult
from standard_annotation_backend.domain.ontology import (
    OntologyClosureRow,
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
    OntologyTerm,
)
from standard_annotation_backend.ontology.registry import GO_DEFINITION
from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    JobRecord,
    OntologyClosureRecord,
    OntologyMetadataRecord,
    OntologyTermRecord,
)
from standard_annotation_backend.persistence.repositories import (
    OntologyRepository,
    OntologySnapshotPrunedError,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.ontology_load_service import (
    OntologyLoadService,
)

JOB_ID = UUID("00000000-0000-0000-0000-000000000701")
NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
PRUNED_AT = NOW + timedelta(hours=1)


def _stage_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> tuple[UUID, OntologyDocument, OntologySnapshot]:
    with session_factory() as session:
        session.add(
            JobRecord(
                job_id=JOB_ID,
                job_type="ontology_load",
                status="failed",
                requested_by="test",
                parameters={"ontology": "go"},
                progress={},
                warnings=[],
                error="Ontology load failed",
                created_at=NOW,
                updated_at=NOW,
                completed_at=NOW,
            )
        )
        session.commit()
    document = OntologyDocument(
        ontology_key=OntologyKey.GO,
        content=b"fixture",
        source_type="test",
        source_locator="fixture/go.obo",
        source_revision="revision",
        source_checksum="a" * 64,
        retrieved_at=NOW,
    )
    snapshot = OntologySnapshot(
        document=document,
        document_version="fixture",
        closure_predicates=(),
        terms={"GO:0000001": OntologyTerm("GO:0000001", False, (), ())},
        edges=(),
    )
    with unit_of_work_factory() as unit_of_work:
        record = unit_of_work.ontologies.stage(
            job_id=JOB_ID,
            document=document,
            snapshot=snapshot,
            closure_rows=(),
        )
        version_id = record.version_id
        record.bulk_data_pruned_at = NOW
        unit_of_work.commit()
    return version_id, document, snapshot


def test_pruned_snapshot_cannot_be_read_as_complete(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Term and closure reads reject a pruned snapshot."""
    version_id, _, _ = _stage_snapshot(unit_of_work_factory, session_factory)

    with unit_of_work_factory() as unit_of_work:
        with pytest.raises(OntologySnapshotPrunedError):
            unit_of_work.ontologies.list_terms(version_id)
        with pytest.raises(OntologySnapshotPrunedError):
            unit_of_work.ontologies.list_closure(version_id)


def test_pruned_snapshot_cannot_be_activated(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Activation rejects a snapshot whose term and closure rows were pruned."""
    version_id, _, _ = _stage_snapshot(unit_of_work_factory, session_factory)

    with (
        unit_of_work_factory() as unit_of_work,
        pytest.raises(OntologySnapshotPrunedError),
    ):
        unit_of_work.ontologies.activate(version_id)


def test_pruned_snapshot_cannot_be_resumed_as_complete(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Restaging the same job rejects its metadata-only snapshot."""
    _, document, snapshot = _stage_snapshot(unit_of_work_factory, session_factory)

    service = OntologyLoadService(unit_of_work_factory, GO_DEFINITION)

    with pytest.raises(OntologySnapshotPrunedError):
        service.stage(job_id=JOB_ID, document=document, snapshot=snapshot)


def _job_for_status(job_id: UUID, status: str) -> JobRecord:
    values: dict[str, object] = {}
    if status == "running":
        values["started_at"] = NOW
    elif status == "succeeded":
        values.update(started_at=NOW, completed_at=NOW, result={})
    elif status == "failed":
        values.update(completed_at=NOW, error="Ontology load failed")
    return JobRecord(
        job_id=job_id,
        job_type="ontology_load",
        status=status,
        requested_by="test",
        parameters={"ontology": "go"},
        progress={},
        warnings=[],
        created_at=NOW,
        updated_at=NOW,
        **values,
    )


def _stage_history(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> dict[str, UUID]:
    states = (
        ("queued_candidate", "queued"),
        ("running_candidate", "running"),
        ("old_success", "succeeded"),
        ("previous_success", "succeeded"),
        ("active_success", "succeeded"),
        ("failed_candidate", "failed"),
    )
    job_ids = {
        name: UUID(f"00000000-0000-0000-0000-{index:012d}")
        for index, (name, _) in enumerate(states, start=801)
    }
    with session_factory() as session:
        session.add_all(
            _job_for_status(job_ids[name], status) for name, status in states
        )
        session.commit()

    version_ids: dict[str, UUID] = {}
    with unit_of_work_factory() as unit_of_work:
        for index, (name, status) in enumerate(states, start=1):
            term_id = f"GO:{index:07d}"
            broader_id = f"GO:{index + 100:07d}"
            document = OntologyDocument(
                ontology_key=OntologyKey.GO,
                content=name.encode(),
                source_type="test",
                source_locator="fixture/go.obo",
                source_revision=name,
                source_checksum=f"{index:x}" * 64,
                retrieved_at=NOW + timedelta(minutes=index),
            )
            snapshot = OntologySnapshot(
                document=document,
                document_version=name,
                closure_predicates=("rdfs:subClassOf",),
                terms={
                    term_id: OntologyTerm(term_id, False, (), ()),
                    broader_id: OntologyTerm(broader_id, False, (), ()),
                },
                edges=(),
            )
            record = unit_of_work.ontologies.stage(
                job_id=job_ids[name],
                document=document,
                snapshot=snapshot,
                closure_rows=(
                    OntologyClosureRow(
                        term_id,
                        "rdfs:subClassOf",
                        broader_id,
                        1,
                    ),
                ),
            )
            version_ids[name] = record.version_id
            if status == "succeeded":
                unit_of_work.ontologies.activate(record.version_id)
                load_result: dict[str, object] = {"source_revision": name}
                record.load_result = load_result
        unit_of_work.audit.record(
            action=AuditAction.ONTOLOGY_LOADED,
            actor_id="test",
            result=AuditResult.SUCCESS,
            job_id=job_ids["old_success"],
        )
        unit_of_work.audit.record(
            action=AuditAction.JOB_FAILED,
            actor_id="test",
            result=AuditResult.SUCCESS,
            job_id=job_ids["failed_candidate"],
        )
        unit_of_work.commit()
    return version_ids


def test_pruning_keeps_required_snapshots_and_preserves_provenance(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Pruning deletes eligible term and closure rows but retains provenance."""
    version_ids = _stage_history(unit_of_work_factory, session_factory)
    service = OntologyLoadService(unit_of_work_factory, GO_DEFINITION)

    pruned = service.prune(pruned_at=PRUNED_AT)

    names_by_version = {version_id: name for name, version_id in version_ids.items()}
    assert tuple(names_by_version[version_id] for version_id in pruned) == (
        "old_success",
        "failed_candidate",
    )
    with unit_of_work_factory() as unit_of_work:
        for version_id in pruned:
            with pytest.raises(OntologySnapshotPrunedError):
                unit_of_work.ontologies.list_terms(version_id)
            with pytest.raises(OntologySnapshotPrunedError):
                unit_of_work.ontologies.list_closure(version_id)
        for name in (
            "previous_success",
            "active_success",
            "queued_candidate",
            "running_candidate",
        ):
            version_id = version_ids[name]
            assert len(unit_of_work.ontologies.list_terms(version_id)) == 2
            assert len(unit_of_work.ontologies.list_closure(version_id)) == 1
    with session_factory() as session:
        metadata = {
            record.version_id: record
            for record in session.scalars(select(OntologyMetadataRecord))
        }
        assert len(metadata) == 6
        assert metadata[version_ids["old_success"]].load_result == {
            "source_revision": "old_success"
        }
        assert {
            version_id
            for version_id, record in metadata.items()
            if record.bulk_data_pruned_at == PRUNED_AT
        } == set(pruned)
        for version_id in pruned:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OntologyTermRecord)
                    .where(OntologyTermRecord.version_id == version_id)
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OntologyClosureRecord)
                    .where(OntologyClosureRecord.version_id == version_id)
                )
                == 0
            )
        for name in (
            "previous_success",
            "active_success",
            "queued_candidate",
            "running_candidate",
        ):
            version_id = version_ids[name]
            assert metadata[version_id].bulk_data_pruned_at is None
        assert session.scalar(select(func.count()).select_from(JobRecord)) == 6
        assert session.scalar(select(func.count()).select_from(AuditEventRecord)) == 2


def test_repeated_pruning_is_harmless_and_preserves_original_timestamp(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Pruning records absent rows once, and a retry preserves the original time."""
    version_ids = _stage_history(unit_of_work_factory, session_factory)
    service = OntologyLoadService(unit_of_work_factory, GO_DEFINITION)
    with session_factory() as session:
        session.execute(
            delete(OntologyClosureRecord).where(
                OntologyClosureRecord.version_id == version_ids["failed_candidate"]
            )
        )
        session.execute(
            delete(OntologyTermRecord).where(
                OntologyTermRecord.version_id == version_ids["failed_candidate"]
            )
        )
        session.commit()
    first = service.prune(pruned_at=PRUNED_AT)

    repeated = service.prune(pruned_at=PRUNED_AT + timedelta(hours=1))

    assert version_ids["failed_candidate"] in first
    assert repeated == ()
    with session_factory() as session:
        records = {
            record.version_id: record
            for record in session.scalars(select(OntologyMetadataRecord))
        }
        assert records[version_ids["old_success"]].bulk_data_pruned_at == PRUNED_AT
        assert records[version_ids["failed_candidate"]].bulk_data_pruned_at == PRUNED_AT


def test_bulk_read_refreshes_cached_metadata_after_concurrent_pruning(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A term read refreshes cached metadata and detects committed pruning."""
    version_ids = _stage_history(unit_of_work_factory, session_factory)
    with session_factory() as reader:
        repository = OntologyRepository(reader)
        cached = reader.get(OntologyMetadataRecord, version_ids["old_success"])
        assert cached is not None
        assert cached.bulk_data_pruned_at is None

        OntologyLoadService(unit_of_work_factory, GO_DEFINITION).prune(
            pruned_at=PRUNED_AT
        )

        with pytest.raises(OntologySnapshotPrunedError):
            repository.list_terms(version_ids["old_success"])


def test_bulk_read_lock_prevents_concurrent_pruning(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Pruning waits until a transaction reading snapshot data ends."""
    version_ids = _stage_history(unit_of_work_factory, session_factory)
    pruning_started = Event()

    def prune() -> tuple[UUID, ...]:
        pruning_started.set()
        return OntologyLoadService(unit_of_work_factory, GO_DEFINITION).prune(
            pruned_at=PRUNED_AT
        )

    with session_factory() as reader:
        repository = OntologyRepository(reader)
        assert len(repository.list_terms(version_ids["old_success"])) == 2
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(prune)
            assert pruning_started.wait(timeout=5)
            try:
                with pytest.raises(FutureTimeoutError):
                    future.result(timeout=1)
            finally:
                reader.rollback()
            assert version_ids["old_success"] in future.result(timeout=5)
