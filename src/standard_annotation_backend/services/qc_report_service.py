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

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.auth import (
    AuthorizationContext,
    AuthorizationScope,
    PermissionAction,
    PermissionDeniedError,
    authorize_role,
)
from standard_annotation_backend.domain.duplicate_policy import duplicate_key
from standard_annotation_backend.domain.jobs import (
    TERMINAL_JOB_STATUSES,
    JobNotFoundError,
    JobStatus,
    JobType,
    group_scope,
    group_scope_parameters,
)
from standard_annotation_backend.domain.qc_reports import (
    STORED_QC_REPORT_RESULT,
    IssueType,
    QcFinding,
    QcGroupCounts,
    QcReportFindingsExpiredError,
    QcReportNotCompletedError,
    QcReportNotFoundError,
    QcReportResult,
    ReferenceDataVersions,
)
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    RefreshKindName,
)
from standard_annotation_backend.domain.schema_artifacts import SCHEMA_PACKAGE_VERSION
from standard_annotation_backend.domain.semantic_validation.facts import (
    ReferenceDataUnavailableError,
)
from standard_annotation_backend.domain.semantic_validation.rules import (
    validate_semantics,
)
from standard_annotation_backend.domain.validation import (
    ValidationIssue,
    validate_annotation,
)
from standard_annotation_backend.persistence.locks import AdvisoryTryLock, LockNamespace
from standard_annotation_backend.persistence.models import (
    JobRecord,
    QcReportFindingRecord,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.audit_service import AuditService
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

logger = logging.getLogger(__name__)

_BATCH_SIZE = 500
_FAILURE_MESSAGE = "QC report failed"


def queue_qc_report(
    unit_of_work: SqlAlchemyUnitOfWork, *, group_key: str | None, requested_by: str
) -> Job:
    """Return the queued QC report for a scope, creating one if none is queued.

    A lock held until the caller's transaction ends lets only one caller at a
    time look for or create a report. A queued report that is found stays locked
    for the same time, so it cannot start until the caller that reused it
    commits. A report that is already running is never reused; a new report is
    queued instead. The caller must commit and then send the job to a worker.

    Args:
        unit_of_work: The caller's transaction.
        group_key: The group to report on, or `None` for all groups.
        requested_by: The actor recorded as the requester of a new report.

    Returns:
        The queued report, either new or already queued.
    """
    unit_of_work.jobs.lock_qc_report_starts()
    parameters = group_scope_parameters(group_key)
    record = unit_of_work.jobs.find_queued(
        job_type=JobType.ANNOTATION_QC_REPORT, parameters=parameters
    )
    if record is None:
        record = unit_of_work.jobs.create(
            job_type=JobType.ANNOTATION_QC_REPORT,
            requested_by=requested_by,
            parameters=parameters,
            now=datetime.now(UTC),
        )
        AuditService(unit_of_work.audit).record_job_lifecycle(
            action=AuditAction.JOB_QUEUED, record=record
        )
    return job_from_record(record)


class QcReportBusyError(RuntimeError):
    """Report that another QC report of the same scope is running.

    The report stays queued; the Celery task retries it later.
    """

    def __init__(self) -> None:
        super().__init__("another QC report for this scope is running")


class QcReportRunner:
    """Run QC report jobs.

    Args:
        unit_of_work_factory: Opens the transactions for reading and storing.
        sources: Configured sources, which name the ontologies and their prefixes.
        try_lock: Takes a PostgreSQL advisory lock only if it is free. The runner
            locks the job, so only one process runs it, and the report's scope,
            so only one report of a scope runs at a time.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        sources: RefreshSources,
        try_lock: AdvisoryTryLock,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._try_lock = try_lock
        self._jobs = JobService(unit_of_work_factory)
        self._ontologies = {
            key: sources.ontology_definition(key).identifier_prefixes
            for key in sources.keys(RefreshKindName.ONTOLOGY)
        }

    def run(self, job_id: UUID) -> None:
        """Run one QC report to completion or failure.

        Nothing happens if another process is running the report or the report
        has finished. A report whose job parameters are invalid fails with
        `invalid_parameters`. A report that finds required reference data
        missing fails with `reference_data_unavailable`, and its findings and
        counts are deleted. A report that starts again after an interruption
        first deletes the findings and counts it stored earlier. When the report
        succeeds, the findings of every older report of the same scope are
        deleted in the transaction that records the success.

        Raises:
            QcReportBusyError: If another report of the same scope is running.
                The report stays queued.
        """
        with self._try_lock(LockNamespace.JOB, job_id) as acquired:
            if not acquired:
                return
            try:
                job = self._jobs.find(job_id)
            except JobNotFoundError:
                return
            if job.status in TERMINAL_JOB_STATUSES:
                return
            try:
                if job.job_type is not JobType.ANNOTATION_QC_REPORT:
                    raise ValueError("not a QC report job")
                group_key = group_scope(job.parameters)
            except ValueError:
                self._fail(job_id, RefreshFailureCode.INVALID_PARAMETERS)
                return
            with self._try_lock(LockNamespace.QC_REPORT, group_key) as free:
                if not free:
                    raise QcReportBusyError
                job = self._jobs.start(job_id)
                with self._unit_of_work_factory() as unit_of_work:
                    unit_of_work.qc_reports.clear(job_id)
                    unit_of_work.commit()
                try:
                    result = self._check(job, group_key)
                except ReferenceDataUnavailableError:
                    self._fail(job_id, RefreshFailureCode.REFERENCE_DATA_UNAVAILABLE)
                    return
                self._jobs.update_progress(
                    job_id, progress={**_progress(result), "phase": "completed"}
                )
                parameters = dict(job.parameters)
                self._jobs.succeed(
                    job_id,
                    result=result.to_job_result(),
                    finalize=lambda unit_of_work: (
                        unit_of_work.qc_reports.expire_findings_of_older_reports(
                            job_id, parameters
                        )
                    ),
                )

    def _check(self, job: Job, group_key: str | None) -> QcReportResult:
        """Validate every active annotation of the scope, batch by batch."""
        checked = 0
        with_issues = 0
        # Resolved before the first batch, so a scope with no annotations still
        # fails when reference data is missing, and records what it checked against.
        with self._unit_of_work_factory() as unit_of_work:
            snapshots = unit_of_work.validation_facts.active_snapshots(self._ontologies)
            reference_data: list[ReferenceDataVersions] = [
                unit_of_work.validation_facts.reference_versions(snapshots)
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
                snapshots = unit_of_work.validation_facts.active_snapshots(
                    self._ontologies
                )
                versions = unit_of_work.validation_facts.reference_versions(snapshots)
                if versions not in reference_data:
                    reference_data.append(versions)
                creators = unit_of_work.annotations.creators(
                    [record.annotation_id for record in records]
                )
                issues_by_id: dict[UUID, tuple[ValidationIssue, ...]] = {}
                valid: list[tuple[UUID, Annotation]] = []
                for record in records:
                    validation = validate_annotation(record.annotation_data)
                    if validation.annotation is None:
                        issues_by_id[record.annotation_id] = validation.errors
                    else:
                        valid.append((record.annotation_id, validation.annotation))
                peers = unit_of_work.annotations.duplicate_peers_for(
                    {
                        annotation_id: duplicate_key(annotation)
                        for annotation_id, annotation in valid
                    }
                )
                facts = unit_of_work.validation_facts.gather(
                    [annotation for _, annotation in valid],
                    [peers[annotation_id] for annotation_id, _ in valid],
                    self._ontologies,
                    snapshots=snapshots,
                )
                for (annotation_id, annotation), annotation_facts in zip(
                    valid, facts, strict=True
                ):
                    issues = validate_semantics(annotation, annotation_facts)
                    if issues:
                        issues_by_id[annotation_id] = issues
                findings = [
                    QcFinding(
                        annotation_id=record.annotation_id,
                        annotation_version=record.current_version,
                        owning_group_id=record.owning_group_id,
                        # Every annotation has a first version; the default
                        # only keeps the type a string.
                        created_by=creators.get(record.annotation_id, ""),
                        issues=issues_by_id[record.annotation_id],
                    )
                    for record in records
                    if record.annotation_id in issues_by_id
                ]
                unit_of_work.qc_reports.record_batch(
                    job.job_id,
                    findings,
                    Counter(record.owning_group_id for record in records),
                )
                unit_of_work.commit()
            checked += len(records)
            with_issues += len(findings)
            self._jobs.update_progress(
                job.job_id,
                progress={
                    "phase": "checking",
                    "annotations_checked": checked,
                    "annotations_with_issues": with_issues,
                },
            )
        return QcReportResult(
            group_key=group_key,
            annotations_checked=checked,
            annotations_with_issues=with_issues,
            reference_data=tuple(reference_data),
            sabe_version=get_application_version(),
            schema_version=SCHEMA_PACKAGE_VERSION,
        )

    def _fail(self, job_id: UUID, code: RefreshFailureCode) -> None:
        self._jobs.fail_refresh(
            job_id,
            error=_FAILURE_MESSAGE,
            failure_code=code,
            cleanup=lambda unit_of_work: unit_of_work.qc_reports.clear(job_id),
        )


def _progress(result: QcReportResult) -> dict[str, object]:
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
        sending the job fails, the error is logged and the job stays queued; the
        next start for the same scope reuses it and sends it again.

        Args:
            context: The caller's authorization.
            owning_group_id: The group to report on, if the caller names one.

        Returns:
            The queued report.

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
            job = queue_qc_report(
                unit_of_work, group_key=group_key, requested_by=context.actor_id
            )
            unit_of_work.commit()
        try:
            self._dispatch(job)
        except Exception as error:
            # The job stays queued; the next start of the same scope reuses it
            # and sends it again.
            logger.error(
                "QC report dispatch failed: job_id=%s failure_type=%s",
                job.job_id,
                type(error).__name__,
            )
        return job

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
            owning_group_id=group_scope(job.parameters),
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
        issue_type: IssueType | None,
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
        owning_group_id=group_scope(job.parameters),
        findings_available=(
            job.status is JobStatus.SUCCEEDED and job.job_id not in superseded
        ),
    )


def _finding(record: QcReportFindingRecord) -> QcFinding:
    """Build a finding from its stored row."""
    issues: list[ValidationIssue] = []
    for stored in record.issues:
        issue = ValidationIssue(
            location=tuple(stored["location"]),
            message=stored["message"],
            type=stored["type"],
        )
        if "details" in stored:
            issue["details"] = stored["details"]
        issues.append(issue)
    return QcFinding(
        annotation_id=record.annotation_id,
        annotation_version=record.annotation_version,
        owning_group_id=record.owning_group_id,
        created_by=record.created_by,
        issues=tuple(issues),
    )
