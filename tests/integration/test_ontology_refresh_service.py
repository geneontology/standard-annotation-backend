"""Verify how ontology refreshes load, check, and activate snapshots."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest
from refresh_helpers import (
    GO_ENTRY,
    TEST_SOURCES,
    OboTerm,
    go_document,
    ignore_progress,
    obo,
    source_document,
    sources_with,
    start_job,
)
from seeding import insert_annotation
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    ChangeSource,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.ontology import (
    GO_ONTOLOGY_KEY,
    OntologyCatalog,
    OntologyDocument,
    OntologySnapshot,
    OntologyTerm,
)
from standard_annotation_backend.domain.refresh import TerminalRefreshError
from standard_annotation_backend.domain.semantic_validation.facts import (
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.stored_json import StoredDataError
from standard_annotation_backend.persistence.locks import bind_try_lock
from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    OntologyMetadataRecord,
)
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
        ontology_key=GO_ONTOLOGY_KEY,
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
                change_source=ChangeSource.API,
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


def test_activation_leaves_annotations_unchanged(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """Activation only makes the snapshot active; annotations keep their terms.

    The annotation still uses the term that the new snapshot replaces, at
    version 1, and the only audit event is `ontology.refreshed`.
    """
    annotation_id = UUID("00000000-0000-0000-0000-000000000043")
    _prepare(
        unit_of_work_factory,
        annotations=((annotation_id, _annotation(OLD_TERM)),),
        old_terms={OLD_TERM: OntologyTerm(OLD_TERM, False, (), ())},
    )
    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    service = OntologyRefreshService(
        unit_of_work_factory, TEST_SOURCES, bind_try_lock(database_engine), None
    )

    outcome = service.apply(job, go_document(CANDIDATE), ignore_progress)

    assert "annotation_update_count" not in outcome.result
    assert outcome.counts == {"ontology_warning_count": 0}
    with unit_of_work_factory() as uow:
        active = uow.ontologies.get_active(GO_ONTOLOGY_KEY)
        assert active is not None
        assert str(active.version_id) == outcome.result["ontology_version_id"]
        record = uow.annotations.get(annotation_id)
        assert record is not None
        assert record.current_version == 1
        assert record.ontology_class_id == OLD_TERM
    with session_factory() as session:
        assert [audit.action for audit in _refresh_audits(session)] == [
            "ontology.refreshed"
        ]


def test_recovery_rejects_a_stored_result_with_an_invalid_count(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
) -> None:
    """Recovery reports an invalid stored count instead of reading it as zero."""
    annotation_id = UUID("00000000-0000-0000-0000-000000000045")
    _prepare(
        unit_of_work_factory,
        annotations=((annotation_id, _annotation(OLD_TERM)),),
        old_terms={OLD_TERM: OntologyTerm(OLD_TERM, False, (), ())},
    )
    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    service = OntologyRefreshService(
        unit_of_work_factory, TEST_SOURCES, bind_try_lock(database_engine), None
    )
    outcome = service.apply(job, go_document(CANDIDATE), ignore_progress)
    assert service.recover(job) is not None
    with session_factory() as session, session.begin():
        session.execute(
            update(OntologyMetadataRecord)
            .where(OntologyMetadataRecord.job_id == job.job_id)
            .values(refresh_result={**outcome.result, "term_count": "1"})
        )

    with pytest.raises(StoredDataError):
        service.recover(job)


def test_an_ontology_other_than_go_is_read_as_its_entry_describes(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """An ontology loads with the header, prefixes, and closure its entry sets."""
    sources = sources_with(
        ontologies={
            "cl": {
                "type": "https",
                "url": "https://example.org/cl-basic.obo",
                "document_ontology_id": "cl/cl-basic",
                "prefixes": ["CL"],
            }
        }
    )
    content = b"""\
format-version: 1.4
ontology: cl/cl-basic

[Term]
id: CL:0000000

[Term]
id: CL:0000001
is_a: CL:0000000
"""
    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "cl")
    service = OntologyRefreshService(
        unit_of_work_factory,
        sources,
        bind_try_lock(database_engine),
        None,
    )

    outcome = service.apply(job, source_document("cl", content), ignore_progress)

    assert (
        outcome.result["ontology"],
        outcome.result["term_count"],
        outcome.result["closure_count"],
    ) == ("cl", 2, 0)
    with unit_of_work_factory() as uow:
        assert uow.ontologies.get_active("cl") is not None


def test_validation_reads_subsets_and_alternative_identifiers_from_a_refresh(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
    reference_data: Callable[..., None],
) -> None:
    """Validation sees each refreshed term's subsets and each alternative ID's term."""
    content = b"""\
format-version: 1.4
ontology: go
subsetdef: gocheck_do_not_annotate "Term not to be used for direct annotation"
subsetdef: gocheck_obsoletion_candidate "Terms planned for obsoletion"

[Term]
id: GO:0000001
alt_id: GO:0000009
subset: gocheck_do_not_annotate

[Term]
id: GO:0000002
"""
    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    service = OntologyRefreshService(
        unit_of_work_factory, TEST_SOURCES, bind_try_lock(database_engine), None
    )

    service.apply(job, go_document(content), ignore_progress)

    with unit_of_work_factory() as uow:
        tagged, plain, alternative = (
            facts.terms
            for facts in uow.validation_facts.gather(
                [
                    _annotation(term_id)
                    for term_id in ("GO:0000001", "GO:0000002", "GO:0000009")
                ],
                [(), (), ()],
                OntologyCatalog({GO_ONTOLOGY_KEY: ("GO",)}),
            )
        )
    assert tagged["GO:0000001"].subsets == frozenset({"gocheck_do_not_annotate"})
    assert plain["GO:0000002"].subsets == frozenset()
    assert alternative["GO:0000009"] == TermFact(
        TermStatus.ALTERNATE, primary_id="GO:0000001"
    )


def test_a_go_file_without_the_rule_subsets_is_refused(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """GO must declare the subsets GO Rules check, or nothing is loaded.

    The job fails as an invalid document and no snapshot is staged or activated.
    """
    content = b"""\
format-version: 1.4
ontology: go
subsetdef: gocheck_do_not_annotate "Term not to be used for direct annotation"

[Term]
id: GO:0000001
"""
    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    service = OntologyRefreshService(
        unit_of_work_factory, TEST_SOURCES, bind_try_lock(database_engine), None
    )

    with pytest.raises(TerminalRefreshError) as raised:
        service.apply(job, go_document(content), ignore_progress)

    assert raised.value.failure_code == "invalid_document"
    assert "gocheck_obsoletion_candidate" in str(raised.value.failure_details)
    with unit_of_work_factory() as uow:
        assert uow.ontologies.get_by_job(job.job_id) is None
        assert uow.ontologies.get_active(GO_ONTOLOGY_KEY) is None


@pytest.mark.parametrize(
    "change",
    [
        {"closure_predicates": ["rdfs:subClassOf"]},
        {"prefixes": ["GO", "GOCHE"]},
    ],
    ids=["closure-predicates", "prefixes"],
)
def test_an_unchanged_file_is_loaded_again_when_its_definition_changes(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
    change: dict[str, object],
) -> None:
    """A change to an ontology's entry applies on the next refresh.

    The refresh loads the file again even though the file itself has not changed.
    """
    content = obo(OboTerm("GO:0000001"), OboTerm("GO:0000002", is_a=("GO:0000001",)))
    lock = bind_try_lock(database_engine)
    first = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    OntologyRefreshService(unit_of_work_factory, TEST_SOURCES, lock, None).apply(
        first, go_document(content), ignore_progress
    )
    changed = sources_with(ontologies={"go": {**GO_ENTRY, **change}})
    second = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")

    outcome = OntologyRefreshService(unit_of_work_factory, changed, lock, None).apply(
        second, go_document(content), ignore_progress
    )

    assert outcome.unchanged is False
    with unit_of_work_factory() as uow:
        active = uow.ontologies.get_active(GO_ONTOLOGY_KEY)
        assert active is not None
        assert active.job_id == second.job_id
        assert active.loaded_predicates == list(
            changed.ontology_definition(GO_ONTOLOGY_KEY).closure_predicates
        )
