"""Verify that QC reports validate every annotation in scope and keep findings."""

from uuid import UUID

import pytest
from refresh_helpers import (
    OboTerm,
    build_scan_runner,
    obo,
    refresh_go,
    refreshing_factory,
)
from seeding import active_snapshot, deactivate_reference_data, insert_annotation
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_qc_report_repository import _finding

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.qc_reports import STORED_QC_REPORT_RESULT
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.persistence.models import AnnotationRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_scan import queue_or_join
from standard_annotation_backend.services.job_service import Job, JobService


@pytest.fixture(autouse=True)
def _reference(active_annotation_subjects: None) -> None:
    """Load the subjects and reference data that valid annotations need."""


def _annotation(**changes: object) -> Annotation:
    return Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:P12345",
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0006915",
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


def _queue(unit_of_work_factory: UnitOfWorkFactory, group_key: str | None) -> Job:
    with unit_of_work_factory() as uow:
        job = queue_or_join(
            uow,
            job_type=JobType.ANNOTATION_QC_REPORT,
            group_key=group_key,
            requested_by="scheduler",
        ).job
        uow.commit()
    return job


def _run(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
    group_key: str | None = None,
) -> Job:
    job = _queue(unit_of_work_factory, group_key)
    build_scan_runner(database_engine, unit_of_work_factory).run(job.job_id)
    return JobService(unit_of_work_factory).find(job.job_id)


def _findings(
    unit_of_work_factory: UnitOfWorkFactory, job_id: UUID
) -> dict[UUID, list[str]]:
    with unit_of_work_factory() as uow:
        page = uow.qc_reports.findings_page(
            job_id,
            owning_group_id=None,
            created_by=None,
            issue_type=None,
            limit=200,
            offset=0,
        )
        return {
            record.annotation_id: [issue["type"] for issue in record.issues]
            for record in page.items
        }


def test_only_annotations_with_issues_get_findings_and_counts_cover_all(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A clean annotation is counted but gets no finding; duplicates list each other.

    The job result records the totals and the versions checked against, and each
    of two duplicates in different groups names the other as its peer.
    """
    _insert(session_factory, _annotation(references=["PMID:5"]))
    first = _insert(session_factory, _annotation())
    second = _insert(session_factory, _annotation(), group="RGD")
    unknown = _insert(
        session_factory,
        _annotation(db_object_id="UniProtKB:NOPE", references=["PMID:6"]),
    )

    job = _run(unit_of_work_factory, database_engine)

    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None
    assert job.result["annotations_checked"] == 4
    assert job.result["annotations_with_issues"] == 3
    assert job.result["sabe_version"]
    assert job.result["schema_version"]
    reference_data = job.result["reference_data"]
    assert isinstance(reference_data, list)
    assert len(reference_data) == 1
    assert _findings(unit_of_work_factory, job.job_id) == {
        first: ["duplicate_annotation"],
        second: ["duplicate_annotation"],
        unknown: ["unknown_entity"],
    }
    with unit_of_work_factory() as uow:
        duplicates = uow.qc_reports.findings_page(
            job.job_id,
            owning_group_id=None,
            created_by=None,
            issue_type="duplicate_annotation",
            limit=10,
            offset=0,
        ).items
        assert {
            record.annotation_id: record.issues[0]["details"] for record in duplicates
        } == {
            first: {"peer_ids": [str(second)]},
            second: {"peer_ids": [str(first)]},
        }
        counts = uow.qc_reports.group_counts(job.job_id, None)
        assert counts["MGI"].annotations_checked == 3
        assert counts["RGD"].annotations_with_issues == 1
        assert uow.qc_reports.issue_counts(job.job_id, None) == {
            "unknown_entity": 1,
            "duplicate_annotation": 2,
        }


def test_a_group_report_checks_only_that_group(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """Annotations of other groups are neither counted nor given findings."""
    mgi = _insert(session_factory, _annotation(db_object_id="UniProtKB:NOPE"))
    _insert(
        session_factory,
        _annotation(db_object_id="UniProtKB:NOPE", references=["PMID:2"]),
        group="RGD",
    )

    job = _run(unit_of_work_factory, database_engine, group_key="MGI")

    assert job.result is not None
    assert job.result["owning_group_id"] == "MGI"
    assert job.result["annotations_checked"] == 1
    assert list(_findings(unit_of_work_factory, job.job_id)) == [mgi]


def test_a_structurally_invalid_stored_annotation_is_a_structural_finding(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A stored annotation that fails the schema gets a `structural` finding.

    The finding holds the schema's issues, and the report still succeeds.
    """
    broken = _insert(session_factory, _annotation())
    with session_factory.begin() as session:
        record = session.get(AnnotationRecord, broken)
        assert record is not None
        record.annotation_data = {**record.annotation_data, "evidence_type": "nonsense"}

    job = _run(unit_of_work_factory, database_engine)

    assert job.status is JobStatus.SUCCEEDED
    with unit_of_work_factory() as uow:
        assert uow.qc_reports.issue_counts(job.job_id, None) == {"structural": 1}
        (finding,) = uow.qc_reports.findings_page(
            job.job_id,
            owning_group_id=None,
            created_by=None,
            issue_type="structural",
            limit=10,
            offset=0,
        ).items
        assert finding.annotation_id == broken
        assert finding.issues[0]["location"][:2] == ["annotation", "evidence_type"]


def test_missing_reference_data_fails_the_report_and_leaves_no_rows(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """With no active GO snapshot the report fails with `reference_data_unavailable`.

    It keeps no findings or counts.
    """
    _insert(session_factory, _annotation(db_object_id="UniProtKB:NOPE"))
    deactivate_reference_data(session_factory, SnapshotKind.ONTOLOGY, "go")

    job = _run(unit_of_work_factory, database_engine)

    assert job.status is JobStatus.FAILED
    assert job.progress["failure_code"] == "reference_data_unavailable"
    assert _findings(unit_of_work_factory, job.job_id) == {}
    with unit_of_work_factory() as uow:
        assert uow.qc_reports.group_counts(job.job_id, None) == {}


def test_a_redelivered_running_report_starts_from_empty_rows(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A report run again after an interruption discards the earlier run's rows.

    Its findings and counts cover each annotation once.
    """
    _insert(session_factory, _annotation(db_object_id="UniProtKB:NOPE"))
    job = _queue(unit_of_work_factory, None)
    JobService(unit_of_work_factory).start(job.job_id)
    with unit_of_work_factory() as uow:
        uow.qc_reports.record_batch(
            job.job_id, [_finding(9, "MGI", "unknown_entity")], {"MGI": 7}
        )
        uow.commit()

    build_scan_runner(database_engine, unit_of_work_factory).run(job.job_id)

    assert JobService(unit_of_work_factory).find(job.job_id).status is (
        JobStatus.SUCCEEDED
    )
    with unit_of_work_factory() as uow:
        counts = uow.qc_reports.group_counts(job.job_id, None)
        assert counts["MGI"].annotations_checked == 1
    assert UUID(int=9) not in _findings(unit_of_work_factory, job.job_id)


def test_an_empty_report_succeeds_and_records_no_reference_data(
    unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine
) -> None:
    """A report with nothing to check succeeds and lists no reference data."""
    empty = _run(unit_of_work_factory, database_engine, group_key="ZFIN")

    assert empty.status is JobStatus.SUCCEEDED
    assert empty.result is not None
    assert empty.result["annotations_checked"] == 0
    assert empty.result["reference_data"] == []


def test_a_refresh_before_the_first_batch_lists_only_the_batch_set(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A report lists the set its first batch used, not the set active at its start."""
    _insert(session_factory, _annotation())
    job = _queue(unit_of_work_factory, None)
    # The scan's first unit of work checks reference data is available; the
    # second reads the first batch.
    scan_factory = refreshing_factory(
        unit_of_work_factory,
        before_call=2,
        refresh=lambda: refresh_go(
            database_engine, unit_of_work_factory, obo(OboTerm("GO:0006915"))
        ),
    )

    build_scan_runner(
        database_engine,
        unit_of_work_factory,
        scan_unit_of_work_factory=scan_factory,
    ).run(job.job_id)

    finished = JobService(unit_of_work_factory).find(job.job_id)
    assert finished.status is JobStatus.SUCCEEDED
    assert finished.result is not None
    (versions,) = STORED_QC_REPORT_RESULT.load(finished.result).reference_data
    active = active_snapshot(session_factory, SnapshotKind.ONTOLOGY, "go")
    assert active is not None
    assert [
        ontology.version_id
        for ontology in versions.ontologies
        if ontology.ontology_key == "go"
    ] == [active.snapshot_id]


def test_an_abandoned_report_keeps_no_findings_or_counts(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """Rows a stopped report stored are deleted when it is abandoned."""
    _insert(session_factory, _annotation(db_object_id="UniProtKB:NOPE"))
    stuck = _queue(unit_of_work_factory, None)
    JobService(unit_of_work_factory).start(stuck.job_id)
    with unit_of_work_factory() as uow:
        uow.qc_reports.record_batch(
            stuck.job_id, [_finding(9, "MGI", "unknown_entity")], {"MGI": 7}
        )
        uow.commit()

    _run(unit_of_work_factory, database_engine)

    assert _findings(unit_of_work_factory, stuck.job_id) == {}
    with unit_of_work_factory() as uow:
        assert uow.qc_reports.group_counts(stuck.job_id, None) == {}
