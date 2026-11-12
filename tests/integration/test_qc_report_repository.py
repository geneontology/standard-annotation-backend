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
from standard_annotation_backend.persistence.models import (
    JobRecord,
    QcReportIssueCountRecord,
)
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
            "unknown_entity": 1,
            "gorule_0000055": 2,
        }
        assert uow.qc_reports.issue_counts(report, None)["structural"] == 1
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


def _succeed(
    session_factory: sessionmaker[Session], completed: dict[UUID, datetime]
) -> None:
    """Mark reports succeeded at the given times, as the runner would."""
    with session_factory.begin() as session:
        for job_id, completed_at in completed.items():
            job = session.get(JobRecord, job_id)
            assert job is not None
            job.status = JobStatus.SUCCEEDED
            job.started_at = completed_at
            job.completed_at = completed_at
            job.result = {}


def _record_one_finding(
    unit_of_work_factory: UnitOfWorkFactory, *job_ids: UUID
) -> None:
    with unit_of_work_factory() as uow:
        for job_id in job_ids:
            uow.qc_reports.record_batch(
                job_id, [_finding(1, "MGI", "unknown_entity")], {"MGI": 1}
            )
        uow.commit()


def test_a_later_report_expires_earlier_findings_of_its_scope_only(
    unit_of_work_factory: UnitOfWorkFactory, session_factory: sessionmaker[Session]
) -> None:
    """Earlier reports of the scope lose findings; other scopes and counts remain."""
    older = _report(unit_of_work_factory, {})
    other_scope = _report(unit_of_work_factory, {"group_key": "MGI"})
    newer = _report(unit_of_work_factory, {})
    start = datetime.now(UTC)
    _succeed(
        session_factory,
        {
            older: start,
            other_scope: start + timedelta(hours=1),
            newer: start + timedelta(hours=2),
        },
    )
    _record_one_finding(unit_of_work_factory, older, other_scope, newer)

    with unit_of_work_factory() as uow:
        uow.qc_reports.expire_findings_of_older_reports(newer, {})
        uow.commit()

    totals = [
        _total(unit_of_work_factory, job_id) for job_id in (older, other_scope, newer)
    ]
    assert totals == [0, 1, 1]
    with unit_of_work_factory() as uow:
        assert uow.qc_reports.group_counts(older, None)["MGI"].annotations_checked == 1
        assert uow.qc_reports.has_newer_report(older, {}, start) is True
        assert (
            uow.qc_reports.has_newer_report(newer, {}, start + timedelta(hours=2))
            is False
        )


def test_a_report_stamped_earlier_never_expires_one_stamped_later(
    unit_of_work_factory: UnitOfWorkFactory, session_factory: sessionmaker[Session]
) -> None:
    """Expiry follows completion order, even when the earlier-stamped report finishes last.

    This happens when two workers' clocks differ. The later-stamped report stays
    current, and the earlier-stamped one reads as replaced.
    """
    stamped_later = _report(unit_of_work_factory, {})
    stamped_earlier = _report(unit_of_work_factory, {})
    start = datetime.now(UTC)
    _succeed(
        session_factory,
        {stamped_later: start + timedelta(minutes=5), stamped_earlier: start},
    )
    _record_one_finding(unit_of_work_factory, stamped_later, stamped_earlier)

    with unit_of_work_factory() as uow:
        uow.qc_reports.expire_findings_of_older_reports(stamped_earlier, {})
        uow.commit()

    assert _total(unit_of_work_factory, stamped_later) == 1
    with unit_of_work_factory() as uow:
        assert uow.qc_reports.has_newer_report(stamped_earlier, {}, start) is True
        assert (
            uow.qc_reports.has_newer_report(
                stamped_later, {}, start + timedelta(minutes=5)
            )
            is False
        )


def test_reports_completed_at_the_same_time_are_ordered_by_id(
    unit_of_work_factory: UnitOfWorkFactory, session_factory: sessionmaker[Session]
) -> None:
    """With equal completion times, exactly one report of the scope is current."""
    first, second = sorted(
        (_report(unit_of_work_factory, {}), _report(unit_of_work_factory, {}))
    )
    finished = datetime.now(UTC)
    _succeed(session_factory, {first: finished, second: finished})
    _record_one_finding(unit_of_work_factory, first, second)

    with unit_of_work_factory() as uow:
        uow.qc_reports.expire_findings_of_older_reports(second, {})
        uow.qc_reports.expire_findings_of_older_reports(first, {})
        uow.commit()

    totals = (_total(unit_of_work_factory, first), _total(unit_of_work_factory, second))
    assert totals == (0, 1)
    with unit_of_work_factory() as uow:
        assert uow.qc_reports.has_newer_report(first, {}, finished) is True
        assert uow.qc_reports.has_newer_report(second, {}, finished) is False


def test_counts_of_an_issue_type_no_longer_in_the_catalog_are_still_read(
    unit_of_work_factory: UnitOfWorkFactory, session_factory: sessionmaker[Session]
) -> None:
    """Counts are permanent, so a type later dropped from the catalog is listed last."""
    report = _report(unit_of_work_factory, {})
    _record_one_finding(unit_of_work_factory, report)
    with session_factory.begin() as session:
        session.add(
            QcReportIssueCountRecord(
                job_id=report,
                owning_group_id="MGI",
                issue_type="gorule_0000099",
                annotation_count=4,
            )
        )

    with unit_of_work_factory() as uow:
        assert list(uow.qc_reports.issue_counts(report, None).items()) == [
            ("unknown_entity", 1),
            ("gorule_0000099", 4),
        ]
