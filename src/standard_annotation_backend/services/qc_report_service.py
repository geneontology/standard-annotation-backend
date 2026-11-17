"""Queue, run, and read annotation QC reports.

A QC report validates every active annotation in its scope, which is either all
groups or one owning group, with the same checks that client writes use. It
stores one finding for each annotation with issues. It reads annotations in
batches without locking them, so an annotation edited during the run may be
checked as it was before or after the edit. Each batch is checked against the
reference data snapshots (the stored versions of ontologies, GO Rule data, and
entity catalogs) that are active when the batch is read, and the report records
every set of versions it used.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from standard_annotation_backend.domain.auth import (
    AuthorizationContext,
    AuthorizationScope,
    PermissionAction,
    PermissionDeniedError,
    authorize_role,
)
from standard_annotation_backend.domain.jobs import (
    JobStatus,
    JobType,
    ProgressReporter,
    scan_groups,
)
from standard_annotation_backend.domain.ontology import OntologyCatalog
from standard_annotation_backend.domain.qc_reports import (
    STORED_QC_REPORT_RESULT,
    STORED_VALIDATION_ISSUE,
    QcFinding,
    QcGroupCounts,
    QcReportFindingsExpiredError,
    QcReportNotCompletedError,
    QcReportNotFoundError,
    QcReportResult,
    ReferenceDataVersions,
    issue_types_of,
)
from standard_annotation_backend.domain.schema_artifacts import SCHEMA_PACKAGE_VERSION
from standard_annotation_backend.persistence.models import (
    JobRecord,
    QcReportFindingRecord,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.annotation_scan import (
    ScanOutcome,
    queue_or_join,
)
from standard_annotation_backend.services.annotation_validator import (
    AnnotationValidator,
    Candidate,
)
from standard_annotation_backend.services.job_service import (
    Job,
    JobService,
    job_from_record,
)
from standard_annotation_backend.services.pagination import ResultPage
from standard_annotation_backend.services.resource_authorization import (
    ownership_scope,
)
from standard_annotation_backend.version import get_application_version

_BATCH_SIZE = 500


class QcReportScan:
    """Validate every active annotation a report covers, and store its findings.

    `QcReportScan` is the annotation scan for `annotation_qc_report` jobs (see
    `services/annotation_scan.py`).

    Args:
        unit_of_work_factory: Opens the transactions for reading and storing.
        ontologies: The configured ontologies and the identifier prefixes each owns.
    """

    def __init__(
        self, unit_of_work_factory: UnitOfWorkFactory, ontologies: OntologyCatalog
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._ontologies = ontologies
        self._validator = AnnotationValidator(ontologies)

    @property
    def job_type(self) -> JobType:
        """Return `annotation_qc_report`."""
        return JobType.ANNOTATION_QC_REPORT

    @property
    def failure_message(self) -> str:
        """Return "QC report failed"."""
        return "QC report failed"

    def prepare(self, unit_of_work: SqlAlchemyUnitOfWork, job: Job) -> None:
        """Delete findings and counts an earlier attempt of this report stored."""
        unit_of_work.qc_reports.clear(job.job_id)

    def execute(
        self, job: Job, group_key: str | None, report: ProgressReporter
    ) -> ScanOutcome:
        """Validate every active annotation of `group_key`, or of all groups."""
        result = self._check(job, group_key, report)
        return ScanOutcome(result=result.to_job_result(), counts=_progress(result))

    def finalize(self, unit_of_work: SqlAlchemyUnitOfWork, job: Job) -> None:
        """Delete the findings of older reports covering the same groups.

        This runs in the transaction that records the report's success, so
        readers move from the older findings to the newer ones in one step.
        """
        unit_of_work.qc_reports.expire_findings_of_older_reports(
            job.job_id, dict(job.parameters)
        )

    def cleanup(self, unit_of_work: SqlAlchemyUnitOfWork, job: Job) -> None:
        """Delete the findings and counts of a failed report."""
        unit_of_work.qc_reports.clear(job.job_id)

    def _check(
        self, job: Job, group_key: str | None, report: ProgressReporter
    ) -> QcReportResult:
        """Validate every active annotation of the scope, batch by batch."""
        checked = 0
        with_issues = 0
        # Resolved before the first batch, so a scope with no annotations still
        # fails when reference data is missing, and records what it checked against.
        with self._unit_of_work_factory() as unit_of_work:
            snapshots = unit_of_work.snapshots.resolve(self._ontologies)
            reference_data: list[ReferenceDataVersions] = [
                unit_of_work.snapshots.reference_versions(snapshots)
            ]
        after: UUID | None = None
        while True:
            with self._unit_of_work_factory() as unit_of_work:
                records = unit_of_work.annotations.list_active_after(
                    after, owning_group_id=group_key, limit=_BATCH_SIZE
                )
                if not records:
                    break
                after = records[-1].annotation_id
                snapshots = unit_of_work.snapshots.resolve(self._ontologies)
                versions = unit_of_work.snapshots.reference_versions(snapshots)
                if versions not in reference_data:
                    reference_data.append(versions)
                creators = unit_of_work.annotations.creators(
                    [record.annotation_id for record in records]
                )
                results = self._validator.check(
                    unit_of_work,
                    [
                        Candidate(record.annotation_data, record.annotation_id)
                        for record in records
                    ],
                    snapshots=snapshots,
                    include_report_only=True,
                )
                findings = [
                    QcFinding(
                        annotation_id=record.annotation_id,
                        annotation_version=record.current_version,
                        owning_group_id=record.owning_group_id,
                        # Every annotation has a first version; the default
                        # only keeps the type a string.
                        created_by=creators.get(record.annotation_id, ""),
                        issues=result.issues,
                        issue_types=issue_types_of(result.stage, result.issues),
                    )
                    for record, result in zip(records, results, strict=True)
                    if result.issues
                ]
                unit_of_work.qc_reports.record_batch(
                    job.job_id,
                    findings,
                    Counter(record.owning_group_id for record in records),
                )
                unit_of_work.commit()
            checked += len(records)
            with_issues += len(findings)
            report(
                "checking",
                {
                    "annotations_checked": checked,
                    "annotations_with_issues": with_issues,
                },
            )
        return QcReportResult(
            owning_group_id=group_key,
            annotations_checked=checked,
            annotations_with_issues=with_issues,
            reference_data=tuple(reference_data),
            sabe_version=get_application_version(),
            schema_version=SCHEMA_PACKAGE_VERSION,
        )


def _progress(result: QcReportResult) -> dict[str, int]:
    """Return a report's counts in job-progress form."""
    return {
        "annotations_checked": result.annotations_checked,
        "annotations_with_issues": result.annotations_with_issues,
    }


@dataclass(frozen=True, slots=True)
class QcReportSummary:
    """Describe a report as one reader may see it.

    Counts cover only the groups the reader may see: their own group, or every
    group for a reader with global scope. While a report runs, its counts are
    those recorded so far.

    Attributes:
        job: The report's job.
        owning_group_id: The group the report covers, or `None` for all groups.
        annotations_checked: Annotations validated, in the visible groups.
        annotations_with_issues: Annotations with at least one issue, in the
            visible groups.
        by_issue_type: The number of visible annotations with each issue type,
            keyed by issue type value.
        by_group: The counts of each visible group.
        result: The stored result once the report has succeeded, otherwise
            `None`.
    """

    job: Job
    owning_group_id: str | None
    annotations_checked: int
    annotations_with_issues: int
    by_issue_type: dict[str, int] = field(default_factory=dict)
    by_group: dict[str, QcGroupCounts] = field(default_factory=dict)
    result: QcReportResult | None = None


@dataclass(frozen=True, slots=True)
class QcReportListing:
    """Describe one report in a list of reports.

    Attributes:
        job: The report's job.
        owning_group_id: The group the report covers, or `None` for all groups.
        findings_available: Whether the report succeeded and no later report of
            its scope has replaced its findings.
    """

    job: Job
    owning_group_id: str | None
    findings_available: bool


class QcReportService:
    """Start QC reports and read them within each caller's scope.

    Args:
        unit_of_work_factory: Opens transactions.
        dispatch: Sends a queued report to a worker after the job is committed.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        dispatch: Callable[[Job], None],
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._dispatch = dispatch

    def start(self, context: AuthorizationContext, owning_group_id: str | None) -> Job:
        """Queue a report, or reuse the scope's queued one, and send it to a worker.

        A global admin reports on one group or, without `owning_group_id`, on all
        groups. An admin limited to one group always reports on that group. If
        sending the report to a worker fails, a report this call created is
        returned failed with `dispatch_failed`; a reused report is left queued.

        Args:
            context: The caller's authorization.
            owning_group_id: The group to report on, if the caller names one.

        Returns:
            The queued report, or the failed one if it could not be sent.

        Raises:
            PermissionDeniedError: If the caller is not an admin, or names a
                group other than its own.
        """
        authorize_role(context, PermissionAction.QC_REPORT_CREATE)
        if context.scope is AuthorizationScope.GLOBAL:
            group_key = owning_group_id
        elif context.group_id is None or owning_group_id not in (
            None,
            context.group_id,
        ):
            raise PermissionDeniedError
        else:
            group_key = context.group_id
        with self._unit_of_work_factory() as unit_of_work:
            queued = queue_or_join(
                unit_of_work,
                job_type=JobType.ANNOTATION_QC_REPORT,
                group_key=group_key,
                requested_by=context.actor_id,
            )
            unit_of_work.commit()
        return JobService(self._unit_of_work_factory).send(
            queued.job, created=queued.created, dispatch=self._dispatch
        )

    def list(
        self,
        context: AuthorizationContext,
        *,
        status: JobStatus | None,
        limit: int,
        offset: int,
    ) -> ResultPage[QcReportListing]:
        """Return one page of the reports covering the caller's group, newest first.

        A global reader sees every report. Other readers see all-groups reports
        and their own group's reports.

        Raises:
            PermissionDeniedError: If the caller's authorization cannot read
                reports.
        """
        authorize_role(context, PermissionAction.QC_REPORT_READ)
        group = ownership_scope(context).owning_group_id
        with self._unit_of_work_factory() as unit_of_work:
            page = unit_of_work.qc_reports.list_reports(
                covering_group=group, status=status, limit=limit, offset=offset
            )
            superseded = unit_of_work.qc_reports.superseded(
                [record.job_id for record in page.items]
            )
            return ResultPage.from_page(
                page,
                lambda record: _listing(record, superseded),
                limit=limit,
                offset=offset,
            )

    def summary(
        self, context: AuthorizationContext, report_id: UUID
    ) -> QcReportSummary:
        """Return a report's status, versions, and counts for the caller's groups.

        Raises:
            QcReportNotFoundError: If `report_id` names no QC report.
            PermissionDeniedError: If the caller's authorization cannot read
                reports.
        """
        authorize_role(context, PermissionAction.QC_REPORT_READ)
        group = ownership_scope(context).owning_group_id
        with self._unit_of_work_factory() as unit_of_work:
            job = _report(unit_of_work, report_id)
            groups = unit_of_work.qc_reports.group_counts(report_id, group)
            issues = unit_of_work.qc_reports.issue_counts(report_id, group)
        return QcReportSummary(
            job=job,
            owning_group_id=scan_groups(job.parameters),
            annotations_checked=sum(
                counts.annotations_checked for counts in groups.values()
            ),
            annotations_with_issues=sum(
                counts.annotations_with_issues for counts in groups.values()
            ),
            by_issue_type=issues,
            by_group=groups,
            result=(
                STORED_QC_REPORT_RESULT.load(job.result)
                if job.status is JobStatus.SUCCEEDED and job.result is not None
                else None
            ),
        )

    def findings(
        self,
        context: AuthorizationContext,
        report_id: UUID,
        *,
        issue_type: str | None,
        owning_group_id: str | None,
        limit: int,
        offset: int,
    ) -> ResultPage[QcFinding]:
        """Return one page of a report's findings within the caller's ownership scope.

        A caller with `self` scope sees findings only for the annotations they
        created in their group, a caller with `group` scope sees their group's
        findings, and a caller with `global` scope sees every finding. A group
        filter outside the caller's scope returns an empty page.

        Args:
            context: The caller's authorization.
            report_id: The report.
            issue_type: Only findings with an issue of this type, if given.
            owning_group_id: Only this group's findings, if given.
            limit: The most findings to return.
            offset: The number of findings to skip.

        Raises:
            QcReportNotFoundError: If `report_id` names no QC report.
            QcReportNotCompletedError: If the report has not succeeded.
            QcReportFindingsExpiredError: If a newer report of the same scope
                has replaced its findings.
            PermissionDeniedError: If the caller's authorization cannot read
                reports.
        """
        authorize_role(context, PermissionAction.QC_REPORT_READ)
        scope = ownership_scope(context)
        with self._unit_of_work_factory() as unit_of_work:
            job = _report(unit_of_work, report_id)
            if job.status is not JobStatus.SUCCEEDED or job.completed_at is None:
                raise QcReportNotCompletedError(report_id)
            if scope.owning_group_id is not None and owning_group_id not in (
                None,
                scope.owning_group_id,
            ):
                # A filter for a group outside the caller's scope matches nothing.
                page = None
            else:
                page = unit_of_work.qc_reports.findings_page(
                    report_id,
                    owning_group_id=scope.owning_group_id or owning_group_id,
                    created_by=scope.created_by,
                    issue_type=issue_type,
                    limit=limit,
                    offset=offset,
                )
            # Checked after reading: a newer report deletes these findings and
            # records its success in one commit, so a page that missed them is
            # always followed by a check that sees the newer report.
            if unit_of_work.qc_reports.has_newer_report(
                report_id, dict(job.parameters), job.completed_at
            ):
                raise QcReportFindingsExpiredError(report_id)
            if page is None:
                return ResultPage(items=(), total=0, limit=limit, offset=offset)
            return ResultPage.from_page(page, _finding, limit=limit, offset=offset)


def _report(unit_of_work: SqlAlchemyUnitOfWork, report_id: UUID) -> Job:
    """Return a QC report's job, treating any other job as missing."""
    record = unit_of_work.jobs.get(report_id)
    if record is None or record.job_type is not JobType.ANNOTATION_QC_REPORT:
        raise QcReportNotFoundError(report_id)
    return job_from_record(record)


def _listing(record: JobRecord, superseded: set[UUID]) -> QcReportListing:
    """Build a list entry from a report's job record."""
    job = job_from_record(record)
    return QcReportListing(
        job=job,
        owning_group_id=scan_groups(job.parameters),
        findings_available=(
            job.status is JobStatus.SUCCEEDED and job.job_id not in superseded
        ),
    )


def _finding(record: QcReportFindingRecord) -> QcFinding:
    """Build a finding from its stored row.

    Raises:
        StoredDataError: If a stored issue does not have the issue shape.
    """
    return QcFinding(
        annotation_id=record.annotation_id,
        annotation_version=record.annotation_version,
        owning_group_id=record.owning_group_id,
        created_by=record.created_by,
        issues=tuple(STORED_VALIDATION_ISSUE.load(issue) for issue in record.issues),
        issue_types=tuple(record.issue_types),
    )
