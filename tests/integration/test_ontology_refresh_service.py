"""Verify atomic ontology activation and annotation updates."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from refresh_helpers import (
    REVISION,
    OboTerm,
    go_document,
    ignore_progress,
    obo,
    start_job,
)
from seeding import insert_annotation
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.ontology import (
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
    OntologyTerm,
)
from standard_annotation_backend.persistence.annotation_data import (
    AnnotationPersistenceData,
)
from standard_annotation_backend.persistence.locks import bind_try_lock
from standard_annotation_backend.persistence.models import (
    AnnotationOrigin,
    AnnotationRecord,
    AuditEventRecord,
)
from standard_annotation_backend.persistence.repositories import AnnotationRepository
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshService,
)

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
OLD_TERM = "GO:0000001"
NEW_TERM = "GO:0000002"
CANDIDATE = obo(
    OboTerm(OLD_TERM, obsolete=True, replaced_by=(NEW_TERM,)),
    OboTerm(NEW_TERM),
)


def _old_snapshot(terms: dict[str, OntologyTerm]) -> OntologySnapshot:
    document = OntologyDocument(
        ontology_key=OntologyKey.GO,
        content=b"old",
        source_type="test",
        source_locator="fixture/go.obo",
        source_revision="old",
        source_checksum="a" * 64,
        fetched_at=NOW,
    )
    return OntologySnapshot(document, "old", (), terms, ())


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


def _prepare(
    unit_of_work_factory: UnitOfWorkFactory,
    *,
    annotations: tuple[tuple[UUID, Annotation], ...],
    old_terms: dict[str, OntologyTerm],
) -> None:
    """Activate an old snapshot directly and create the annotations it covers."""
    old_job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    old_snapshot = _old_snapshot(old_terms)
    with unit_of_work_factory() as uow:
        old = uow.ontologies.stage(
            job_id=old_job.job_id,
            document=old_snapshot.document,
            snapshot=old_snapshot,
            closure_rows=(),
        )
        uow.ontologies.activate(old.version_id)
        for annotation_id, annotation in annotations:
            insert_annotation(
                uow.annotations.session,
                annotation=annotation,
                actor_id="creator",
                change_source="test",
                owning_group_id="group-1",
                record_origin=AnnotationOrigin.DIRECT,
                annotation_id=annotation_id,
            )
        uow.commit()


def _refresh_audits(session: Session) -> tuple[AuditEventRecord, ...]:
    """Return audit events other than job lifecycle events, oldest first."""
    return tuple(
        session.scalars(
            select(AuditEventRecord)
            .where(AuditEventRecord.action.not_like("job.%"))
            .order_by(AuditEventRecord.created_at)
        )
    )


def test_activation_replaces_all_occurrences_once_and_records_audits(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """Activation commits one version and audit for a multi-field replacement."""
    annotation_id = UUID("00000000-0000-0000-0000-000000000043")
    _prepare(
        unit_of_work_factory,
        annotations=((annotation_id, _annotation(OLD_TERM)),),
        old_terms={OLD_TERM: OntologyTerm(OLD_TERM, False, (), ())},
    )
    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    service = OntologyRefreshService(
        unit_of_work_factory, bind_try_lock(database_engine), None
    )

    outcome = service.apply(job, go_document(CANDIDATE), ignore_progress)

    assert outcome.result["annotation_scan_count"] == 1
    assert outcome.result["annotation_update_count"] == 1
    assert outcome.result["annotation_skip_count"] == 0
    with unit_of_work_factory() as uow:
        active = uow.ontologies.get_active(OntologyKey.GO)
        assert active is not None
        assert str(active.version_id) == outcome.result["ontology_version_id"]
        assert active.source_revision == REVISION
        record = uow.annotations.get(annotation_id)
        assert record is not None
        assert record.current_version == 2
        assert record.ontology_class_id == NEW_TERM
        updated = Annotation.model_validate(record.annotation_data)
        assert updated.annotation_extensions is not None
        assert updated.annotation_extensions[0].extension_term == NEW_TERM
        assert updated.annotation_extensions[1].extension_term == "CL:0000000"
        versions = uow.annotations.list_versions_page(
            annotation_id, limit=100, offset=0
        ).items
        assert len(versions) == 2
        assert versions[-1].change_source == "ontology_refresh"
    with session_factory() as session:
        audits = _refresh_audits(session)
        assert [audit.action for audit in audits] == [
            "annotation.updated",
            "ontology.refreshed",
        ]
        assert all(audit.job_id == job.job_id for audit in audits)
        assert audits[0].annotation_id == annotation_id
        assert audits[0].annotation_version == 2


def test_activation_failure_rolls_back_snapshot_versions_and_audits(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write failure preserves the prior active snapshot and annotation state."""
    annotation_id = UUID("00000000-0000-0000-0000-000000000044")
    _prepare(
        unit_of_work_factory,
        annotations=((annotation_id, _annotation(OLD_TERM)),),
        old_terms={OLD_TERM: OntologyTerm(OLD_TERM, False, (), ())},
    )
    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    service = OntologyRefreshService(
        unit_of_work_factory, bind_try_lock(database_engine), None
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
        service.apply(job, go_document(CANDIDATE), ignore_progress)

    with unit_of_work_factory() as uow:
        active = uow.ontologies.get_active(OntologyKey.GO)
        assert active is not None
        assert active.source_revision == "old"
        record = uow.annotations.get(annotation_id)
        assert record is not None
        assert record.current_version == 1
        assert record.ontology_class_id == OLD_TERM
    with session_factory() as session:
        assert _refresh_audits(session) == ()
