"""Verify batch duplicate safety for ontology-driven annotation updates."""

from datetime import UTC, datetime
from uuid import UUID

from refresh_helpers import OboTerm, go_document, ignore_progress, obo, start_job
from seeding import insert_annotation
from sqlalchemy import Engine

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    ChangeSource,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.ontology import (
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
    OntologyTerm,
)
from standard_annotation_backend.persistence.locks import bind_try_lock
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshService,
)

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)
TERM_A = "GO:0000001"
TERM_B = "GO:0000002"
TERM_X = "GO:0000004"


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
                change_source=ChangeSource.API,
                owning_group_id="group-1",
                record_origin=AnnotationOrigin.DIRECT,
                annotation_id=annotation_id,
            )
        uow.commit()


def test_replacements_that_would_duplicate_each_other_are_both_skipped(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """Annotations whose replacements would become identical are all left unchanged.

    Each annotation gets a `duplicate_conflict` finding naming the other, and
    neither record changes its term or gains a version.
    """
    first_id = UUID("00000000-0000-0000-0000-000000000051")
    second_id = UUID("00000000-0000-0000-0000-000000000052")
    _prepare(
        unit_of_work_factory,
        annotations=(
            (first_id, _annotation(TERM_A)),
            (second_id, _annotation(TERM_B)),
        ),
        old_terms={
            term: OntologyTerm(term, False, (), ()) for term in (TERM_A, TERM_B)
        },
    )
    candidate = obo(
        OboTerm(TERM_A, obsolete=True, replaced_by=(TERM_X,)),
        OboTerm(TERM_B, obsolete=True, replaced_by=(TERM_X,)),
        OboTerm(TERM_X),
    )
    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    service = OntologyRefreshService(
        unit_of_work_factory, bind_try_lock(database_engine), None
    )

    outcome = service.apply(job, go_document(candidate), ignore_progress)

    assert outcome.result["annotation_scan_count"] == 2
    assert outcome.result["annotation_update_count"] == 0
    assert outcome.result["annotation_skip_count"] == 2
    findings = outcome.result["findings"]
    assert isinstance(findings, list)
    duplicate_findings = {
        finding["annotation_id"]: finding
        for finding in findings
        if finding["code"] == "duplicate_conflict"
    }
    assert set(duplicate_findings) == {str(first_id), str(second_id)}
    assert duplicate_findings[str(first_id)]["conflicting_annotation_ids"] == [
        str(second_id)
    ]
    assert duplicate_findings[str(second_id)]["conflicting_annotation_ids"] == [
        str(first_id)
    ]
    with unit_of_work_factory() as uow:
        for annotation_id, expected_term in ((first_id, TERM_A), (second_id, TERM_B)):
            record = uow.annotations.get(annotation_id)
            assert record is not None
            assert record.ontology_class_id == expected_term
            versions = uow.annotations.list_versions_page(
                annotation_id, limit=100, offset=0
            ).items
            assert len(versions) == 1
