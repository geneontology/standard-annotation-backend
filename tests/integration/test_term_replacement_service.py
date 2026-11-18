"""Verify that term replacement jobs replace only what passes validation."""

from collections.abc import Callable, Collection
from uuid import UUID

import pytest
from refresh_helpers import (
    OboTerm,
    build_scan_runner,
    obo,
    refresh_go,
    refreshing_factory,
)
from seeding import (
    SeedTerm,
    active_snapshot,
    deactivate_reference_data,
    insert_annotation,
)
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation, ChangeSource
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.ontology import OntologyCatalog
from standard_annotation_backend.domain.semantic_validation.facts import (
    ActiveSnapshots,
    TermFact,
)
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.domain.term_replacement import (
    STORED_TERM_REPLACEMENT_RESULT,
)
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AuditEventRecord,
)
from standard_annotation_backend.persistence.repositories import (
    ValidationFactsRepository,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_scan import queue_or_join
from standard_annotation_backend.services.job_service import Job, JobService

OBSOLETE = "GO:0000003"
ALTERNATIVE = "GO:0000099"
CURRENT = "GO:0006915"


@pytest.fixture(autouse=True)
def _replacement_terms(
    active_annotation_subjects: None, reference_data: Callable[..., None]
) -> None:
    """Seed an obsolete term and an alternative ID that both lead to `GO:0006915`."""
    reference_data(
        SeedTerm(OBSOLETE, obsolete=True, replaced_by=(CURRENT,)),
        SeedTerm("GO:0006915", alt_ids=(ALTERNATIVE,)),
        SeedTerm("GO:0000004", obsolete=True, replaced_by=("GO:1", "GO:2")),
    )


def _annotation(term_id: str, **changes: object) -> Annotation:
    return Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:P12345",
            "relation": "RO:0002331",
            "ontology_class_id": term_id,
            "references": ["PMID:1"],
            "evidence_type": "ECO:0000314",
            "annotation_date": "2026-11-12",
            "assigned_by": "MGI",
            **changes,
        }
    )


def _insert(
    session_factory: sessionmaker[Session], annotation: Annotation, group: str = "MGI"
) -> UUID:
    with session_factory() as session:
        record = insert_annotation(session, annotation, owning_group_id=group)
        session.commit()
        return record.annotation_id


def _run(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
    group_key: str | None = None,
) -> Job:
    with unit_of_work_factory() as uow:
        job = queue_or_join(
            uow,
            job_type=JobType.TERM_REPLACEMENT,
            group_key=group_key,
            requested_by="curator",
        ).job
        uow.commit()
    build_scan_runner(database_engine, unit_of_work_factory).run(job.job_id)
    return JobService(unit_of_work_factory).find(job.job_id)


def _record(
    session_factory: sessionmaker[Session], annotation_id: UUID
) -> AnnotationRecord:
    with session_factory() as session:
        record = session.get(AnnotationRecord, annotation_id)
        assert record is not None
        session.expunge(record)
        return record


def test_obsolete_terms_and_alternative_ids_are_replaced_by_the_requester(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """Each replaced annotation gets a version by the job's requester, with an audit."""
    obsolete_id = _insert(session_factory, _annotation(OBSOLETE))
    alternative_id = _insert(
        session_factory, _annotation(ALTERNATIVE, references=["PMID:2"])
    )
    untouched_id = _insert(session_factory, _annotation(CURRENT, references=["PMID:3"]))

    job = _run(unit_of_work_factory, database_engine)

    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None
    assert {
        key: job.result[key]
        for key in (
            "annotation_scan_count",
            "annotation_replaced_count",
            "annotation_issue_skip_count",
            "annotation_stale_skip_count",
        )
    } == {
        "annotation_scan_count": 3,
        "annotation_replaced_count": 2,
        "annotation_issue_skip_count": 0,
        "annotation_stale_skip_count": 0,
    }
    (versions,) = STORED_TERM_REPLACEMENT_RESULT.load(job.result).reference_data
    assert "go" in {ontology.ontology_key for ontology in versions.ontologies}
    assert versions.entity_catalogs
    for annotation_id in (obsolete_id, alternative_id):
        record = _record(session_factory, annotation_id)
        assert (record.ontology_class_id, record.current_version) == (CURRENT, 2)
    assert _record(session_factory, untouched_id).current_version == 1
    with unit_of_work_factory() as uow:
        version = uow.annotations.get_version(obsolete_id, 2)
        assert version is not None
        assert version.change_source is ChangeSource.TERM_REPLACEMENT
        assert version.actor_id == "curator"
    with session_factory() as session:
        audits = session.scalars(
            select(AuditEventRecord).where(
                AuditEventRecord.annotation_id == obsolete_id
            )
        ).all()
    assert [
        (audit.action, audit.job_id, audit.annotation_version) for audit in audits
    ] == [("annotation.updated", job.job_id, 2)]


def test_a_chain_of_replacements_is_followed_to_its_current_term(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    reference_data: Callable[..., None],
) -> None:
    """An obsolete term whose replacement is also obsolete gets the chain's last term."""
    reference_data(
        SeedTerm("GO:0000011", obsolete=True, replaced_by=("GO:0000012",)),
        SeedTerm("GO:0000012", obsolete=True, replaced_by=("GO:0000013",)),
        SeedTerm("GO:0000013", parents=("GO:0008150",)),
    )
    annotation_id = _insert(session_factory, _annotation("GO:0000011"))

    job = _run(unit_of_work_factory, database_engine)

    assert job.result is not None
    assert job.result["annotation_replaced_count"] == 1
    record = _record(session_factory, annotation_id)
    assert (record.ontology_class_id, record.current_version) == ("GO:0000013", 2)


def test_a_result_with_issues_is_not_written(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A replacement that leaves another obsolete term, or duplicates an annotation, is skipped."""
    still_obsolete = _insert(
        session_factory,
        _annotation(
            OBSOLETE,
            annotation_extensions=[
                {"extension_relation": "BFO:0000050", "extension_term": "GO:0000004"}
            ],
        ),
    )
    _insert(session_factory, _annotation(CURRENT, references=["PMID:7"]))
    would_duplicate = _insert(
        session_factory, _annotation(OBSOLETE, references=["PMID:7"])
    )

    job = _run(unit_of_work_factory, database_engine)

    assert job.result is not None
    assert job.result["annotation_replaced_count"] == 0
    assert job.result["annotation_issue_skip_count"] == 2
    for annotation_id in (still_obsolete, would_duplicate):
        record = _record(session_factory, annotation_id)
        assert (record.ontology_class_id, record.current_version) == (OBSOLETE, 1)


def test_an_annotation_changed_after_it_was_read_is_skipped_as_stale(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent edit on another connection wins, and the job counts it stale."""
    annotation_id = _insert(session_factory, _annotation(OBSOLETE))
    original = ValidationFactsRepository.terms

    def edit_then_read(
        self: ValidationFactsRepository,
        identifiers: Collection[str],
        ontologies: OntologyCatalog,
        snapshots: ActiveSnapshots,
    ) -> dict[str, TermFact]:
        with session_factory.begin() as other:
            other.execute(
                update(AnnotationRecord)
                .where(AnnotationRecord.annotation_id == annotation_id)
                .values(current_version=AnnotationRecord.current_version + 1)
            )
        return original(self, identifiers, ontologies, snapshots)

    monkeypatch.setattr(ValidationFactsRepository, "terms", edit_then_read)
    job = _run(unit_of_work_factory, database_engine)

    assert job.result is not None
    assert job.result["annotation_stale_skip_count"] == 1
    assert job.result["annotation_replaced_count"] == 0
    assert _record(session_factory, annotation_id).ontology_class_id == OBSOLETE


def test_a_stored_annotation_that_cannot_be_read_is_skipped(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """Structurally invalid stored data is counted with issues; the rest is replaced."""
    broken_id = _insert(session_factory, _annotation(OBSOLETE, references=["PMID:8"]))
    replaced_id = _insert(session_factory, _annotation(OBSOLETE))
    with session_factory.begin() as session:
        record = session.get(AnnotationRecord, broken_id)
        assert record is not None
        record.annotation_data = {**record.annotation_data, "evidence_type": "nonsense"}

    job = _run(unit_of_work_factory, database_engine)

    assert job.result is not None
    assert job.result["annotation_issue_skip_count"] == 1
    assert job.result["annotation_replaced_count"] == 1
    assert _record(session_factory, replaced_id).ontology_class_id == CURRENT


def test_a_group_job_replaces_only_that_groups_annotations(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """Annotations owned by other groups are not read or changed."""
    mgi = _insert(session_factory, _annotation(OBSOLETE))
    rgd = _insert(
        session_factory, _annotation(OBSOLETE, references=["PMID:9"]), group="RGD"
    )

    job = _run(unit_of_work_factory, database_engine, group_key="MGI")

    assert job.result is not None
    assert job.result["group_key"] == "MGI"
    assert job.result["annotation_scan_count"] == 1
    assert _record(session_factory, mgi).ontology_class_id == CURRENT
    assert _record(session_factory, rgd).ontology_class_id == OBSOLETE


def test_missing_reference_data_fails_the_job_without_changes(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """With no active GO snapshot, the job fails with `reference_data_unavailable`."""
    annotation_id = _insert(session_factory, _annotation(OBSOLETE))
    deactivate_reference_data(session_factory, SnapshotKind.ONTOLOGY, "go")

    job = _run(unit_of_work_factory, database_engine)

    assert job.status is JobStatus.FAILED
    assert job.progress["failure_code"] == "reference_data_unavailable"
    assert _record(session_factory, annotation_id).current_version == 1


def test_a_replacement_the_schema_rejects_is_skipped_and_the_job_continues(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    reference_data: Callable[..., None],
) -> None:
    """A target that is not a valid identifier counts as an issue, not a job failure."""
    reference_data(
        SeedTerm("GO:bad id"),
        SeedTerm("GO:0000005", obsolete=True, replaced_by=("GO:bad id",)),
    )
    rejected_id = _insert(session_factory, _annotation("GO:0000005"))
    replaced_id = _insert(session_factory, _annotation(OBSOLETE, references=["PMID:2"]))

    job = _run(unit_of_work_factory, database_engine)

    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None
    assert job.result["annotation_issue_skip_count"] == 1
    assert job.result["annotation_replaced_count"] == 1
    assert _record(session_factory, rejected_id).current_version == 1
    assert _record(session_factory, replaced_id).ontology_class_id == CURRENT


def _go_version_ids(job: Job) -> list[list[UUID]]:
    """Return the GO snapshot ID of each reference data set a job recorded."""
    assert job.result is not None
    return [
        [
            ontology.version_id
            for ontology in versions.ontologies
            if ontology.ontology_key == "go"
        ]
        for versions in STORED_TERM_REPLACEMENT_RESULT.load(job.result).reference_data
    ]


def _run_refreshing_go_before(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
    before_call: int,
) -> Job:
    """Run a job whose scan sees a new GO snapshot from one transaction on.

    `before_call` counts the scan's transactions from 1. The scan's first transaction checks reference data is available, its second
    reads the first batch, and its third writes the first replacement.
    """
    with unit_of_work_factory() as uow:
        job = queue_or_join(
            uow,
            job_type=JobType.TERM_REPLACEMENT,
            group_key=None,
            requested_by="curator",
        ).job
        uow.commit()
    scan_factory = refreshing_factory(
        unit_of_work_factory,
        before_call=before_call,
        refresh=lambda: refresh_go(
            database_engine,
            unit_of_work_factory,
            obo(
                OboTerm(CURRENT),
                OboTerm(OBSOLETE, obsolete=True, replaced_by=(CURRENT,)),
            ),
        ),
    )
    build_scan_runner(
        database_engine,
        unit_of_work_factory,
        scan_unit_of_work_factory=scan_factory,
    ).run(job.job_id)
    return JobService(unit_of_work_factory).find(job.job_id)


def test_a_job_with_nothing_in_scope_records_no_reference_data(
    unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine
) -> None:
    """A job with no annotations to check succeeds and lists no reference data."""
    job = _run(unit_of_work_factory, database_engine, group_key="ZFIN")

    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None
    assert job.result["reference_data"] == []


def test_a_refresh_before_a_write_lists_the_write_set(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A write validated against newer reference data adds that set to the result."""
    _insert(session_factory, _annotation(OBSOLETE))

    job = _run_refreshing_go_before(unit_of_work_factory, database_engine, 3)

    assert job.status is JobStatus.SUCCEEDED
    active = active_snapshot(session_factory, SnapshotKind.ONTOLOGY, "go")
    assert active is not None
    go_ids = _go_version_ids(job)
    assert len(go_ids) == 2
    assert go_ids[1] == [active.snapshot_id]


def test_a_stale_write_adds_no_reference_data(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write skipped as stale was never validated, so its set is not listed."""
    annotation_id = _insert(session_factory, _annotation(OBSOLETE))
    original = ValidationFactsRepository.terms

    def edit_then_read(
        self: ValidationFactsRepository,
        identifiers: Collection[str],
        ontologies: OntologyCatalog,
        snapshots: ActiveSnapshots,
    ) -> dict[str, TermFact]:
        with session_factory.begin() as other:
            other.execute(
                update(AnnotationRecord)
                .where(AnnotationRecord.annotation_id == annotation_id)
                .values(current_version=AnnotationRecord.current_version + 1)
            )
        return original(self, identifiers, ontologies, snapshots)

    monkeypatch.setattr(ValidationFactsRepository, "terms", edit_then_read)
    job = _run_refreshing_go_before(unit_of_work_factory, database_engine, 3)

    assert job.result is not None
    assert job.result["annotation_stale_skip_count"] == 1
    assert len(_go_version_ids(job)) == 1
