"""Store and read QC report findings and counts in the caller's transaction.

A QC (quality control) report is a job that checks stored annotations. For each
annotation with issues it stores a finding that lists them, and it counts
annotations and issue types by owning group. A report's scope is the set of
annotations it checks: one owning group's, or every group's. The scope is stored
as the job's parameters, `{"group_key": key}` or `{}`, so reports with equal
parameters have the same scope.
"""

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, cast, delete, exists, literal, or_, select, tuple_
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased
from sqlalchemy.types import Text

from standard_annotation_backend.domain.jobs import (
    JobStatus,
    JobType,
    group_scope_parameters,
)
from standard_annotation_backend.domain.qc_reports import (
    ISSUE_TYPES,
    QcFinding,
    QcGroupCounts,
)
from standard_annotation_backend.domain.validation import ValidationIssue
from standard_annotation_backend.persistence.models import (
    JobRecord,
    QcReportFindingRecord,
    QcReportGroupCountRecord,
    QcReportIssueCountRecord,
)
from standard_annotation_backend.persistence.repositories.pagination import (
    Page,
    load_page,
)


class QcReportRepository:
    """Write and read QC report findings and counts.

    Args:
        session: Session owned by the calling unit of work.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def clear(self, job_id: UUID) -> None:
        """Delete a report's findings and counts, when it starts over or fails."""
        for model in (
            QcReportFindingRecord,
            QcReportGroupCountRecord,
            QcReportIssueCountRecord,
        ):
            self.session.execute(delete(model).where(model.job_id == job_id))

    def record_batch(
        self,
        job_id: UUID,
        findings: Sequence[QcFinding],
        checked_by_group: Mapping[str, int],
    ) -> None:
        """Store a batch's findings and add its counts to the report's totals.

        Args:
            job_id: The report's job ID.
            findings: One finding for each annotation in the batch with issues.
            checked_by_group: The number of annotations checked in the batch,
                for each owning group.
        """
        if findings:
            self.session.execute(
                pg_insert(QcReportFindingRecord),
                [
                    {
                        "job_id": job_id,
                        "annotation_id": finding.annotation_id,
                        "annotation_version": finding.annotation_version,
                        "owning_group_id": finding.owning_group_id,
                        "created_by": finding.created_by,
                        "issue_types": list(finding.issue_types),
                        "issues": [_json_issue(issue) for issue in finding.issues],
                    }
                    for finding in findings
                ],
            )
        with_issues = Counter(finding.owning_group_id for finding in findings)
        for group, checked in checked_by_group.items():
            group_statement = pg_insert(QcReportGroupCountRecord).values(
                job_id=job_id,
                owning_group_id=group,
                annotations_checked=checked,
                annotations_with_issues=with_issues[group],
            )
            self.session.execute(
                group_statement.on_conflict_do_update(
                    index_elements=["job_id", "owning_group_id"],
                    set_={
                        "annotations_checked": (
                            QcReportGroupCountRecord.annotations_checked
                            + group_statement.excluded.annotations_checked
                        ),
                        "annotations_with_issues": (
                            QcReportGroupCountRecord.annotations_with_issues
                            + group_statement.excluded.annotations_with_issues
                        ),
                    },
                )
            )
        issue_counts = Counter(
            (finding.owning_group_id, issue_type)
            for finding in findings
            for issue_type in finding.issue_types
        )
        for (group, issue_type), count in issue_counts.items():
            issue_statement = pg_insert(QcReportIssueCountRecord).values(
                job_id=job_id,
                owning_group_id=group,
                issue_type=issue_type,
                annotation_count=count,
            )
            self.session.execute(
                issue_statement.on_conflict_do_update(
                    index_elements=["job_id", "owning_group_id", "issue_type"],
                    set_={
                        "annotation_count": (
                            QcReportIssueCountRecord.annotation_count
                            + issue_statement.excluded.annotation_count
                        )
                    },
                )
            )
        self.session.flush()

    def expire_findings_of_older_reports(
        self, job_id: UUID, parameters: dict[str, object]
    ) -> None:
        """Delete the findings of earlier succeeded reports with the same scope.

        Reports are ordered by completion time, then by job ID, the same order
        `has_newer_report` and `superseded` use, so findings are deleted for
        exactly the reports those methods treat as replaced. Group and issue
        counts are kept. Call this in the transaction that marks `job_id`
        succeeded.

        Args:
            job_id: The report that just succeeded.
            parameters: Its job parameters, which name its scope.
        """
        this = (
            select(JobRecord.completed_at, JobRecord.job_id)
            .where(JobRecord.job_id == job_id)
            .subquery()
        )
        older = (
            select(JobRecord.job_id)
            .join(this, literal(True))
            .where(
                JobRecord.job_type == JobType.ANNOTATION_QC_REPORT,
                JobRecord.status == JobStatus.SUCCEEDED,
                JobRecord.parameters == cast(parameters, JSONB),
                tuple_(JobRecord.completed_at, JobRecord.job_id)
                < tuple_(this.c.completed_at, this.c.job_id),
            )
        )
        self.session.execute(
            delete(QcReportFindingRecord).where(QcReportFindingRecord.job_id.in_(older))
        )

    def has_newer_report(
        self, job_id: UUID, parameters: dict[str, object], completed_at: datetime
    ) -> bool:
        """Return whether a later succeeded report with the same scope exists.

        Reports are ordered by completion time, then by job ID. A later report's
        success deletes this report's findings.

        Args:
            job_id: The report's job ID.
            parameters: Its job parameters, which name its scope.
            completed_at: When the report completed.
        """
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        JobRecord.job_type == JobType.ANNOTATION_QC_REPORT,
                        JobRecord.status == JobStatus.SUCCEEDED,
                        JobRecord.parameters == cast(parameters, JSONB),
                        tuple_(JobRecord.completed_at, JobRecord.job_id)
                        > tuple_(
                            literal(completed_at, JobRecord.completed_at.type),
                            literal(job_id, JobRecord.job_id.type),
                        ),
                    )
                )
            )
        )

    def list_reports(
        self,
        *,
        covering_group: str | None,
        status: JobStatus | None,
        limit: int,
        offset: int,
    ) -> Page[JobRecord]:
        """Return one page of QC reports, newest first.

        Args:
            covering_group: Only reports of all groups or of this group, if
                given; otherwise every report.
            status: Only reports in this state, if given.
            limit: The most reports to return.
            offset: The number of reports to skip.
        """
        statement: Select[tuple[JobRecord]] = select(JobRecord).where(
            JobRecord.job_type == JobType.ANNOTATION_QC_REPORT
        )
        if covering_group is not None:
            statement = statement.where(
                or_(
                    JobRecord.parameters == cast(group_scope_parameters(None), JSONB),
                    JobRecord.parameters
                    == cast(group_scope_parameters(covering_group), JSONB),
                )
            )
        if status is not None:
            statement = statement.where(JobRecord.status == status)
        return load_page(
            self.session,
            statement,
            record_type=JobRecord,
            order_by=(JobRecord.created_at, JobRecord.job_id),
            descending=True,
            limit=limit,
            offset=offset,
        )

    def superseded(self, job_ids: Collection[UUID]) -> set[UUID]:
        """Return the reports in `job_ids` that a later report has replaced.

        A report is replaced when a later report with the same scope succeeds,
        using the same order as `has_newer_report`: completion time, then job ID.
        A report that has not completed is never replaced.
        """
        if not job_ids:
            return set()
        later = aliased(JobRecord)
        return set(
            self.session.scalars(
                select(JobRecord.job_id).where(
                    JobRecord.job_id.in_(list(job_ids)),
                    exists().where(
                        later.job_type == JobType.ANNOTATION_QC_REPORT,
                        later.status == JobStatus.SUCCEEDED,
                        later.parameters == JobRecord.parameters,
                        tuple_(later.completed_at, later.job_id)
                        > tuple_(JobRecord.completed_at, JobRecord.job_id),
                    ),
                )
            )
        )

    def group_counts(
        self, job_id: UUID, owning_group_id: str | None
    ) -> dict[str, QcGroupCounts]:
        """Return each group's annotation counts in a report, ordered by group key.

        Args:
            job_id: The report's job ID.
            owning_group_id: Only this group's counts, if given.
        """
        statement = select(QcReportGroupCountRecord).where(
            QcReportGroupCountRecord.job_id == job_id
        )
        if owning_group_id is not None:
            statement = statement.where(
                QcReportGroupCountRecord.owning_group_id == owning_group_id
            )
        return {
            record.owning_group_id: QcGroupCounts(
                annotations_checked=record.annotations_checked,
                annotations_with_issues=record.annotations_with_issues,
            )
            for record in self.session.scalars(
                statement.order_by(QcReportGroupCountRecord.owning_group_id)
            )
        }

    def issue_counts(self, job_id: UUID, owning_group_id: str | None) -> dict[str, int]:
        """Return the number of annotations in a report with each issue type.

        Types in the issue-type catalog (`ISSUE_TYPES`) come first, in catalog
        order. Counts are kept for every report, even after its findings are
        deleted, so a type that an earlier SABE release recorded and the catalog no
        longer lists is still returned, after them, in alphabetical order.

        Args:
            job_id: The report's job ID.
            owning_group_id: Only this group's annotations, if given.
        """
        statement = select(
            QcReportIssueCountRecord.issue_type,
            QcReportIssueCountRecord.annotation_count,
        ).where(QcReportIssueCountRecord.job_id == job_id)
        if owning_group_id is not None:
            statement = statement.where(
                QcReportIssueCountRecord.owning_group_id == owning_group_id
            )
        totals: Counter[str] = Counter()
        for issue_type, count in self.session.execute(statement):
            totals[issue_type] += count
        catalog = [
            entry.issue_type for entry in ISSUE_TYPES if entry.issue_type in totals
        ]
        others = sorted(set(totals) - set(catalog))
        return {issue_type: totals[issue_type] for issue_type in catalog + others}

    def findings_page(
        self,
        job_id: UUID,
        *,
        owning_group_id: str | None,
        created_by: str | None,
        issue_type: str | None,
        limit: int,
        offset: int,
    ) -> Page[QcReportFindingRecord]:
        """Return one page of a report's findings, in annotation ID order.

        Args:
            job_id: The report's job ID.
            owning_group_id: Only this group's findings, if given.
            created_by: Only findings for annotations this actor created, if given.
            issue_type: Only findings with this issue type, if given.
            limit: The most findings to return.
            offset: The number of findings to skip.
        """
        statement: Select[tuple[QcReportFindingRecord]] = select(
            QcReportFindingRecord
        ).where(QcReportFindingRecord.job_id == job_id)
        if owning_group_id is not None:
            statement = statement.where(
                QcReportFindingRecord.owning_group_id == owning_group_id
            )
        if created_by is not None:
            statement = statement.where(QcReportFindingRecord.created_by == created_by)
        if issue_type is not None:
            statement = statement.where(
                QcReportFindingRecord.issue_types.contains(
                    cast([issue_type], ARRAY(Text))
                )
            )
        return load_page(
            self.session,
            statement,
            record_type=QcReportFindingRecord,
            order_by=(QcReportFindingRecord.annotation_id,),
            limit=limit,
            offset=offset,
        )


def _json_issue(issue: ValidationIssue) -> dict[str, object]:
    """Return an issue in JSON form, with its location as a list."""
    stored: dict[str, object] = {
        "location": list(issue["location"]),
        "message": issue["message"],
        "type": issue["type"],
    }
    if "details" in issue:
        stored["details"] = issue["details"]
    return stored
