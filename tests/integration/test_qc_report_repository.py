"""Verify how QC report findings and counts are stored, read, and expired."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from seeding import create_job
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.qc_reports import (
    IssueType,
    QcFinding,
    QcGroupCounts,
)
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory


def _finding(
    annotation_id: int, group: str, *types: str, creator: str = "curator"
) -> QcFinding:
    return QcFinding(
        annotation_id=UUID(int=annotation_id),
        annotation_version=1,
        owning_group_id=group,
        created_by=creator,
        issues=tuple(
            {"location": ("annotation",), "message": "x", "type": issue_type}
            for issue_type in types
        ),
    )


def _report(
    unit_of_work_factory: UnitOfWorkFactory, parameters: dict[str, object]
) -> UUID:
    return create_job(
        unit_of_work_factory,
        job_type=JobType.ANNOTATION_QC_REPORT,
        parameters=parameters,
    ).job_id


def _total(unit_of_work_factory: UnitOfWorkFactory, job_id: UUID) -> int:
    with unit_of_work_factory() as uow:
        return uow.qc_reports.findings_page(
            job_id,
            owning_group_id=None,
            created_by=None,
            issue_type=None,
            limit=10,
            offset=0,
        ).total


def test_batches_accumulate_counts_by_group_and_issue_type(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Counts add up across batches; findings are listed and filtered."""
    report = _report(unit_of_work_factory, {})
    with unit_of_work_factory() as uow:
        uow.qc_reports.record_batch(
            report,
            [
                _finding(1, "MGI", "unknown_entity", "gorule_0000055"),
                _finding(2, "RGD", "missing"),
            ],
            {"MGI": 5, "RGD": 2},
        )
        uow.qc_reports.record_batch(
            report, [_finding(3, "MGI", "gorule_0000055", creator="other")], {"MGI": 3}
        )
        uow.commit()

    with unit_of_work_factory() as uow:
        assert uow.qc_reports.group_counts(report, None) == {
            "MGI": QcGroupCounts(annotations_checked=8, annotations_with_issues=2),
            "RGD": QcGroupCounts(annotations_checked=2, annotations_with_issues=1),
        }
        assert uow.qc_reports.issue_counts(report, "MGI") == {
            IssueType.UNKNOWN_ENTITY: 1,
            IssueType.GORULE_0000055: 2,
        }
        assert uow.qc_reports.issue_counts(report, None)[IssueType.STRUCTURAL] == 1
        page = uow.qc_reports.findings_page(
            report,
            owning_group_id="MGI",
            created_by="curator",
            issue_type=IssueType.GORULE_0000055,
            limit=10,
            offset=0,
        )
        assert [record.annotation_id for record in page.items] == [UUID(int=1)]
        assert page.total == 1
        assert [issue["type"] for issue in page.items[0].issues] == [
            "unknown_entity",
            "gorule_0000055",
        ]


def test_a_newer_report_of_the_same_scope_expires_older_findings_only(
    unit_of_work_factory: UnitOfWorkFactory, session_factory: sessionmaker[Session]
) -> None:
    """Other scopes keep their findings; every report keeps its counts."""
    older = _report(unit_of_work_factory, {})
    other_scope = _report(unit_of_work_factory, {"group_key": "MGI"})
    newer = _report(unit_of_work_factory, {})
    finished = datetime.now(UTC)
    with session_factory.begin() as session:
        for job_id, completed in (
            (older, finished),
            (other_scope, finished + timedelta(hours=1)),
        ):
            job = session.get(JobRecord, job_id)
            assert job is not None
            job.status = JobStatus.SUCCEEDED
            job.started_at = completed
            job.completed_at = completed
            job.result = {}
    with unit_of_work_factory() as uow:
        for job_id in (older, other_scope, newer):
            uow.qc_reports.record_batch(
                job_id, [_finding(1, "MGI", "unknown_entity")], {"MGI": 1}
            )
        uow.qc_reports.expire_findings_of_other_reports(newer, {})
        uow.commit()

    totals = [
        _total(unit_of_work_factory, job_id) for job_id in (older, other_scope, newer)
    ]
    assert totals == [0, 1, 1]
    with unit_of_work_factory() as uow:
        assert uow.qc_reports.group_counts(older, None)["MGI"].annotations_checked == 1
        # `newer` has not succeeded and `other_scope` covers another scope.
        assert uow.qc_reports.has_newer_report(older, {}, finished) is False
        assert (
            uow.qc_reports.has_newer_report(other_scope, {"group_key": "MGI"}, finished)
            is False
        )
