"""Verify that QC reports validate every annotation in scope and keep findings."""

from uuid import UUID

import pytest
from refresh_helpers import TEST_ONTOLOGIES
from seeding import insert_annotation
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session, sessionmaker
from test_qc_report_repository import _finding

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.jobs import JobStatus
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    bind_try_lock,
    try_advisory_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    OntologyMetadataRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.qc_report_service import (
    QcReportBusyError,
    QcReportRunner,
    queue_qc_report,
)


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


def _runner(
    unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine
) -> QcReportRunner:
    return QcReportRunner(
        unit_of_work_factory, TEST_ONTOLOGIES, bind_try_lock(database_engine)
    )


def _queue(unit_of_work_factory: UnitOfWorkFactory, group_key: str | None) -> Job:
    with unit_of_work_factory() as uow:
        job = queue_qc_report(uow, group_key=group_key, requested_by="scheduler")
        uow.commit()
    return job


def _run(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
    group_key: str | None = None,
) -> Job:
    job = _queue(unit_of_work_factory, group_key)
    _runner(unit_of_work_factory, database_engine).run(job.job_id)
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
    assert job.result["group_key"] == "MGI"
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
    with session_factory.begin() as session:
        session.execute(
            update(OntologyMetadataRecord)
            .where(OntologyMetadataRecord.ontology_key == "go")
            .values(active=False)
        )

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

    _runner(unit_of_work_factory, database_engine).run(job.job_id)

    assert JobService(unit_of_work_factory).find(job.job_id).status is (
        JobStatus.SUCCEEDED
    )
    with unit_of_work_factory() as uow:
        counts = uow.qc_reports.group_counts(job.job_id, None)
        assert counts["MGI"].annotations_checked == 1
    assert UUID(int=9) not in _findings(unit_of_work_factory, job.job_id)


def test_a_report_waits_while_another_report_of_its_scope_runs(
    unit_of_work_factory: UnitOfWorkFactory, database_engine: Engine
) -> None:
    """While another report of the scope runs, the report stays queued.

    The other report holds the scope's lock on its own connection, and the run
    raises `QcReportBusyError` so the worker retries later.
    """
    job = _queue(unit_of_work_factory, None)

    with try_advisory_lock(database_engine, LockNamespace.QC_REPORT) as held:
        assert held
        with pytest.raises(QcReportBusyError):
            _runner(unit_of_work_factory, database_engine).run(job.job_id)

    assert JobService(unit_of_work_factory).find(job.job_id).status is (
        JobStatus.QUEUED
    )


def test_a_report_with_nothing_to_check_still_requires_reference_data(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """An empty scope fails when GO is missing, and otherwise records its versions."""
    empty = _run(unit_of_work_factory, database_engine, group_key="ZFIN")
    assert empty.status is JobStatus.SUCCEEDED
    assert empty.result is not None
    assert empty.result["annotations_checked"] == 0
    reference_data = empty.result["reference_data"]
    assert isinstance(reference_data, list)
    assert len(reference_data) == 1

    with session_factory.begin() as session:
        session.execute(
            update(OntologyMetadataRecord)
            .where(OntologyMetadataRecord.ontology_key == "go")
            .values(active=False)
        )
    failed = _run(unit_of_work_factory, database_engine, group_key="ZFIN")

    assert failed.status is JobStatus.FAILED
    assert failed.progress["failure_code"] == "reference_data_unavailable"
