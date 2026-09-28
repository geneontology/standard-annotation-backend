"""Verify atomic ontology activation and annotation updates."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.ontology import (
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
    OntologyTerm,
)
from standard_annotation_backend.ontology.registry import GO_DEFINITION
from standard_annotation_backend.persistence.annotation_data import (
    AnnotationPersistenceData,
)
from standard_annotation_backend.persistence.models import (
    AnnotationOrigin,
    AnnotationRecord,
    AuditEventRecord,
    JobRecord,
)
from standard_annotation_backend.persistence.repositories import AnnotationRepository
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.ontology_load_service import (
    OntologyCandidateConflictError,
    OntologyLoadService,
)

OLD_JOB_ID = UUID("00000000-0000-0000-0000-000000000041")
LOAD_JOB_ID = UUID("00000000-0000-0000-0000-000000000042")
NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)


def _document(revision: str, *, retrieved_at: datetime = NOW) -> OntologyDocument:
    return OntologyDocument(
        ontology_key=OntologyKey.GO,
        content=revision.encode(),
        source_type="test",
        source_locator="fixture/go.obo",
        source_revision=revision,
        source_checksum=("a" if revision == "old" else "b") * 64,
        retrieved_at=retrieved_at,
    )


def _snapshot(
    revision: str,
    terms: dict[str, OntologyTerm],
    *,
    retrieved_at: datetime = NOW,
) -> OntologySnapshot:
    return OntologySnapshot(
        _document(revision, retrieved_at=retrieved_at), revision, (), terms, ()
    )


def _term(
    term_id: str,
    *,
    obsolete: bool = False,
    replaced_by: tuple[str, ...] = (),
) -> OntologyTerm:
    return OntologyTerm(term_id, obsolete, replaced_by, ())


def _annotation(term_id: str, *, object_id: str = "UniProtKB:P12345") -> Annotation:
    return Annotation.model_validate(
        {
            "db_object_id": object_id,
            "negation": None,
            "relation": "RO:0002331",
            "ontology_class_id": term_id,
            "references": ["PMID:1"],
            "evidence_type": "ECO:0000314",
            "with_or_from": [],
            "interacting_taxon_id": [],
            "annotation_date": "2026-09-28",
            "assigned_by": "GO_Central",
            "annotation_extensions": [
                {
                    "extension_relation": "BFO:0000050",
                    "extension_term": term_id,
                },
                {
                    "extension_relation": "BFO:0000050",
                    "extension_term": "CL:0000000",
                },
            ],
        }
    )


def _job(session: Session, job_id: UUID) -> None:
    session.add(
        JobRecord(
            job_id=job_id,
            job_type="authorization_sync",
            status="queued",
            requested_by="test",
            parameters={},
            progress={},
            warnings=[],
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _prepare(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    *,
    annotations: tuple[tuple[UUID, Annotation], ...],
    old_terms: dict[str, OntologyTerm],
) -> None:
    with session_factory() as session:
        _job(session, OLD_JOB_ID)
        _job(session, LOAD_JOB_ID)
        session.commit()
    old_snapshot = _snapshot("old", old_terms)
    with unit_of_work_factory() as uow:
        old = uow.ontologies.stage(
            job_id=OLD_JOB_ID,
            document=old_snapshot.document,
            snapshot=old_snapshot,
            closure_rows=(),
        )
        uow.ontologies.activate(old.version_id)
        for annotation_id, annotation in annotations:
            uow.annotations.create(
                annotation=annotation,
                actor_id="creator",
                change_source="test",
                owning_group_id="group-1",
                record_origin=AnnotationOrigin.DIRECT,
                annotation_id=annotation_id,
            )
        uow.commit()


def test_activation_replaces_all_occurrences_once_and_records_audits(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Activation commits one version and audit for a multi-field replacement."""
    annotation_id = UUID("00000000-0000-0000-0000-000000000043")
    _prepare(
        unit_of_work_factory,
        session_factory,
        annotations=((annotation_id, _annotation("GO:OLD")),),
        old_terms={"GO:OLD": _term("GO:OLD")},
    )
    candidate = _snapshot(
        "new",
        {
            "GO:OLD": _term("GO:OLD", obsolete=True, replaced_by=("GO:NEW",)),
            "GO:NEW": _term("GO:NEW"),
        },
    )
    service = OntologyLoadService(unit_of_work_factory, GO_DEFINITION)

    version = service.stage(
        job_id=LOAD_JOB_ID,
        document=candidate.document,
        snapshot=candidate,
    )
    result = service.activate(job_id=LOAD_JOB_ID, actor_id="ontology-worker")

    assert version.active is False
    assert result.applied is True
    assert result.annotation_scan_count == 1
    assert result.annotation_update_count == 1
    assert result.annotation_skip_count == 0
    with unit_of_work_factory() as uow:
        active = uow.ontologies.get_active(OntologyKey.GO)
        assert active is not None
        assert active.version_id == version.version_id
        record = uow.annotations.get(annotation_id)
        assert record is not None
        assert record.current_version == 2
        assert record.ontology_class_id == "GO:NEW"
        updated = Annotation.model_validate(record.annotation_data)
        assert updated.annotation_extensions is not None
        assert updated.annotation_extensions[0].extension_term == "GO:NEW"
        assert updated.annotation_extensions[1].extension_term == "CL:0000000"
        versions = uow.annotations.list_versions(annotation_id)
        assert len(versions) == 2
        assert versions[-1].change_source == "ontology_load"
    with session_factory() as session:
        audits = tuple(
            session.scalars(
                select(AuditEventRecord).order_by(AuditEventRecord.created_at)
            )
        )
        assert [audit.action for audit in audits] == [
            "annotation.updated",
            "ontology.loaded",
        ]
        assert all(audit.job_id == LOAD_JOB_ID for audit in audits)
        assert audits[0].annotation_id == annotation_id
        assert audits[0].annotation_version == 2


def test_activation_failure_rolls_back_snapshot_versions_and_audits(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write failure preserves the prior active snapshot and annotation state."""
    annotation_id = UUID("00000000-0000-0000-0000-000000000044")
    _prepare(
        unit_of_work_factory,
        session_factory,
        annotations=((annotation_id, _annotation("GO:OLD")),),
        old_terms={"GO:OLD": _term("GO:OLD")},
    )
    candidate = _snapshot(
        "new",
        {
            "GO:OLD": _term("GO:OLD", obsolete=True, replaced_by=("GO:NEW",)),
            "GO:NEW": _term("GO:NEW"),
        },
    )
    service = OntologyLoadService(unit_of_work_factory, GO_DEFINITION)
    service.stage(
        job_id=LOAD_JOB_ID,
        document=candidate.document,
        snapshot=candidate,
    )
    original = AnnotationRepository.apply_system_update

    def fail_after_update(
        self: AnnotationRepository,
        record: AnnotationRecord,
        persistence_data: AnnotationPersistenceData,
        *,
        actor_id: str,
        change_source: str,
    ) -> AnnotationRecord:
        original(
            self,
            record,
            persistence_data,
            actor_id=actor_id,
            change_source=change_source,
        )
        raise RuntimeError("injected flush failure")

    monkeypatch.setattr(AnnotationRepository, "apply_system_update", fail_after_update)
    with pytest.raises(RuntimeError, match="injected flush failure"):
        service.activate(job_id=LOAD_JOB_ID, actor_id="ontology-worker")

    with unit_of_work_factory() as uow:
        active = uow.ontologies.get_active(OntologyKey.GO)
        assert active is not None
        assert active.source_revision == "old"
        record = uow.annotations.get(annotation_id)
        assert record is not None
        assert record.current_version == 1
        assert record.ontology_class_id == "GO:OLD"
    with session_factory() as session:
        assert session.scalar(select(AuditEventRecord)) is None


def test_staged_job_cannot_switch_to_different_source_bytes(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A retry cannot replace the immutable candidate already owned by its job."""
    with session_factory() as session:
        _job(session, LOAD_JOB_ID)
        session.commit()
    service = OntologyLoadService(unit_of_work_factory, GO_DEFINITION)
    first = _snapshot("first", {"GO:1": _term("GO:1")})
    second = _snapshot("second", {"GO:2": _term("GO:2")})
    service.stage(job_id=LOAD_JOB_ID, document=first.document, snapshot=first)

    with pytest.raises(OntologyCandidateConflictError, match="differs"):
        service.stage(job_id=LOAD_JOB_ID, document=second.document, snapshot=second)


def test_older_staged_job_cannot_replace_a_newer_active_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A delayed retry cannot roll an ontology key back to an older resolution."""
    newer_job_id = UUID("00000000-0000-0000-0000-000000000049")
    with session_factory() as session:
        _job(session, LOAD_JOB_ID)
        _job(session, newer_job_id)
        session.commit()
    service = OntologyLoadService(unit_of_work_factory, GO_DEFINITION)
    older = _snapshot(
        "older", {"GO:1": _term("GO:1")}, retrieved_at=NOW + timedelta(days=1)
    )
    newer = _snapshot(
        "newer", {"GO:1": _term("GO:1")}, retrieved_at=NOW - timedelta(days=1)
    )
    service.stage(job_id=LOAD_JOB_ID, document=older.document, snapshot=older)
    service.stage(job_id=newer_job_id, document=newer.document, snapshot=newer)
    service.activate(job_id=newer_job_id, actor_id="ontology-worker")

    with pytest.raises(OntologyCandidateConflictError, match="newer"):
        service.activate(job_id=LOAD_JOB_ID, actor_id="ontology-worker")

    with unit_of_work_factory() as unit_of_work:
        active = unit_of_work.ontologies.get_active(OntologyKey.GO)
        assert active is not None
        assert active.source_revision == "newer"
