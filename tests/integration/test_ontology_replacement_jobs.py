"""Verify batch duplicate safety for ontology-driven annotation updates."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.ontology import (
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
    OntologyTerm,
)
from standard_annotation_backend.ontology.registry import GO_DEFINITION
from standard_annotation_backend.persistence.models import AnnotationOrigin, JobRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.ontology_load_service import (
    OntologyLoadService,
)

OLD_JOB_ID = UUID("00000000-0000-0000-0000-000000000041")
LOAD_JOB_ID = UUID("00000000-0000-0000-0000-000000000042")
NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)


def _term(
    term_id: str,
    *,
    obsolete: bool = False,
    replaced_by: tuple[str, ...] = (),
) -> OntologyTerm:
    return OntologyTerm(term_id, obsolete, replaced_by, ())


def _snapshot(revision: str, terms: dict[str, OntologyTerm]) -> OntologySnapshot:
    document = OntologyDocument(
        ontology_key=OntologyKey.GO,
        content=revision.encode(),
        source_type="test",
        source_locator="fixture/go.obo",
        source_revision=revision,
        source_checksum=("a" if revision == "old" else "b") * 64,
        retrieved_at=NOW,
    )
    return OntologySnapshot(document, revision, (), terms, ())


def _annotation(term_id: str) -> Annotation:
    return Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:P12345",
            "negation": None,
            "relation": "RO:0002331",
            "ontology_class_id": term_id,
            "references": ["PMID:1"],
            "evidence_type": "ECO:0000314",
            "with_or_from": [],
            "interacting_taxon_id": [],
            "annotation_date": "2026-09-28",
            "assigned_by": "GO_Central",
        }
    )


def _prepare(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    *,
    annotations: tuple[tuple[UUID, Annotation], ...],
    old_terms: dict[str, OntologyTerm],
) -> None:
    with session_factory() as session:
        for job_id in (OLD_JOB_ID, LOAD_JOB_ID):
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


def test_duplicate_rejections_repeat_to_a_fixed_point(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Reverting one conflict also rejects replacements that then conflict."""
    first_id = UUID("00000000-0000-0000-0000-000000000051")
    second_id = UUID("00000000-0000-0000-0000-000000000052")
    third_id = UUID("00000000-0000-0000-0000-000000000053")
    _prepare(
        unit_of_work_factory,
        session_factory,
        annotations=(
            (first_id, _annotation("GO:A")),
            (second_id, _annotation("GO:B")),
            (third_id, _annotation("GO:C")),
        ),
        old_terms={term: _term(term) for term in ("GO:A", "GO:B", "GO:C")},
    )
    candidate = _snapshot(
        "new",
        {
            "GO:A": _term("GO:A", obsolete=True, replaced_by=("GO:X",)),
            "GO:B": _term("GO:B", obsolete=True, replaced_by=("GO:X",)),
            "GO:C": _term("GO:C", obsolete=True, replaced_by=("GO:A",)),
            "GO:X": _term("GO:X"),
        },
    )
    service = OntologyLoadService(unit_of_work_factory, GO_DEFINITION)
    service.stage(
        job_id=LOAD_JOB_ID,
        document=candidate.document,
        snapshot=candidate,
    )

    result = service.activate(job_id=LOAD_JOB_ID, actor_id="ontology-worker")

    assert result.annotation_scan_count == 3
    assert result.annotation_update_count == 0
    assert result.annotation_skip_count == 3
    duplicate_findings = [
        finding for finding in result.findings if finding.code == "duplicate_conflict"
    ]
    assert {finding.annotation_id for finding in duplicate_findings} == {
        first_id,
        second_id,
        third_id,
    }
    assert all(finding.conflicting_annotation_ids for finding in duplicate_findings)
    with unit_of_work_factory() as uow:
        for annotation_id, expected_term in (
            (first_id, "GO:A"),
            (second_id, "GO:B"),
            (third_id, "GO:C"),
        ):
            record = uow.annotations.get(annotation_id)
            assert record is not None
            assert record.ontology_class_id == expected_term
        assert all(
            len(uow.annotations.list_versions(annotation_id)) == 1
            for annotation_id in (first_id, second_id, third_id)
        )
