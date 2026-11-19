"""Queue and run annotation scan jobs.

An annotation scan job reads every active annotation of one owning group, or of
all groups, in batches. Term replacement and QC report jobs are annotation scan
jobs. They share how they are queued and how they run; each job type supplies only
the work that differs.

`queue_or_join` creates or reuses the queued job, and `AnnotationScanRunner` runs
it. The runner holds two PostgreSQL advisory locks while a job runs:

- one keyed by the job's ID, so that a Celery message delivered twice is run by
  only one process;
- one keyed by the job type and the groups the job covers, so that two jobs of
  the same type covering the same groups never run at once.

A job that cannot take the second lock stays queued, and its Celery task retries
it. A job that takes it fails every other running job of the same type covering
the same groups with `abandoned`. No process can still be running those jobs:
every running job holds that lock until it stops, and PostgreSQL releases the
lock when a worker's database connection closes.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import (
    TERMINAL_JOB_STATUSES,
    JobFailureCode,
    JobNotFoundError,
    JobStatus,
    JobType,
    ProgressReporter,
    scan_group_parameters,
    scan_groups,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    ActiveSnapshots,
    ReferenceDataUnavailableError,
)
from standard_annotation_backend.domain.snapshots import ReferenceDataVersions
from standard_annotation_backend.persistence.locks import (
    AdvisoryTryLock,
    LockNamespace,
    scan_lock_value,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import (
    Job,
    JobService,
    job_from_record,
)

logger = logging.getLogger(__name__)

_GENERIC_FAILURE_MESSAGE = "Job failed"


def queue_or_join(
    unit_of_work: SqlAlchemyUnitOfWork,
    *,
    job_type: JobType,
    group_key: str | None,
    requested_by: str,
) -> Job:
    """Return the queued job of a type and owning group, creating one if needed.

    A lock held until the caller's transaction ends lets only one caller at a
    time look for or create a job, so callers that commit at about the same time
    share one queued job. A queued job that is found stays locked for the same
    time, so it cannot start until the caller that joined it commits. A job that
    is already running is never joined, because it may have read annotations and
    reference data from before the caller's change; a new job is queued instead.
    The caller must commit, and only then send the job to a worker.

    Args:
        unit_of_work: The caller's transaction.
        job_type: `term_replacement` or `annotation_qc_report`.
        group_key: The one group to cover, or `None` for all groups.
        requested_by: The actor recorded as the requester of a new job.

    Returns:
        The queued job.
    """
    unit_of_work.jobs.lock_annotation_scan_starts()
    parameters = scan_group_parameters(group_key)
    record = unit_of_work.jobs.find_queued(job_type=job_type, parameters=parameters)
    if record is not None:
        return job_from_record(record)
    record = unit_of_work.jobs.create(
        job_type=job_type,
        requested_by=requested_by,
        parameters=parameters,
        now=datetime.now(UTC),
    )
    AuditService(unit_of_work.audit).record_job_lifecycle(
        action=AuditAction.JOB_QUEUED, record=record
    )
    return job_from_record(record)


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    """Describe what a scan's `execute` did.

    Attributes:
        result: The JSON-compatible result stored on the job.
        counts: The final counts stored as job progress.
    """

    result: dict[str, object]
    counts: Mapping[str, int] = field(default_factory=dict)


class ReferenceDataLog:
    """Record each distinct set of snapshots a scan used, in the order first used.

    A scan looks up the active snapshots again for each batch, and a term
    replacement job for each write, so a scan that runs while reference data is
    refreshed uses more than one set. Only sets that checked annotations are
    recorded; a scan records none when it has nothing to check.
    """

    def __init__(self) -> None:
        self._seen: set[frozenset[UUID]] = set()
        self._versions: list[ReferenceDataVersions] = []

    def record(
        self, unit_of_work: SqlAlchemyUnitOfWork, snapshots: ActiveSnapshots
    ) -> None:
        """Record `snapshots` unless an equal set is already recorded.

        Where a new set of snapshots came from is read in `unit_of_work`, which
        must be the transaction that resolved them. Sets already seen are not
        read again.
        """
        if snapshots.snapshot_ids in self._seen:
            return
        self._seen.add(snapshots.snapshot_ids)
        versions = unit_of_work.snapshots.reference_versions(snapshots)
        if versions not in self._versions:
            self._versions.append(versions)

    def versions(self) -> tuple[ReferenceDataVersions, ...]:
        """Return every recorded set, in the order first used."""
        return tuple(self._versions)


class AnnotationScan(Protocol):
    """Supply the work of one annotation scan job type.

    `AnnotationScanRunner.run` calls `prepare` in the transaction that starts the
    job, `execute` to do the work in the scan's own transactions, and `finalize`
    in the transaction that records success. `cleanup` runs in every transaction
    that fails the job, including when a later job abandons it.
    """

    @property
    def job_type(self) -> JobType:
        """Return the job type this scan runs."""
        ...

    @property
    def failure_message(self) -> str:
        """Return the public error stored on a failed job of this type."""
        ...

    def prepare(self, unit_of_work: SqlAlchemyUnitOfWork, job: Job) -> None:
        """Do work that must commit with the job's start.

        A QC report deletes the rows an earlier attempt of the same job stored.
        """
        ...

    def execute(
        self, job: Job, group_key: str | None, report: ProgressReporter
    ) -> ScanOutcome:
        """Process every active annotation of the groups the job covers.

        The scan opens and commits its own transactions, one or more per batch.

        Args:
            job: The started job.
            group_key: The one group the job covers, or `None` for all groups.
            report: Stores the job's progress after each batch.

        Returns:
            The job's result and final counts.

        Raises:
            ReferenceDataUnavailableError: If required reference data has no
                active snapshot. The runner fails the job.
        """
        ...

    def finalize(self, unit_of_work: SqlAlchemyUnitOfWork, job: Job) -> None:
        """Do work that must commit together with the job's success."""
        ...

    def cleanup(self, unit_of_work: SqlAlchemyUnitOfWork, job: Job) -> None:
        """Remove what a failed job stored, in the transaction that fails it."""
        ...


class AnnotationScanBusyError(RuntimeError):
    """Report that another job of the same type, covering the same groups, is running.

    The job stays queued; the Celery task retries it later.
    """

    def __init__(self) -> None:
        super().__init__("another job of this type and these groups is running")


class AnnotationScanRunner:
    """Run annotation scan jobs of every type.

    `create_job_executors` builds one for each Celery task or CLI run, and
    `JobExecutors` calls `run` for every term replacement or QC report job.

    Args:
        try_lock: Takes a PostgreSQL advisory lock only if it is free.
        jobs: Reads and updates job state.
        unit_of_work_factory: Opens the transactions that start and finish jobs.
        scans: Every annotation scan this runner can run.

    Raises:
        ValueError: If two scans claim the same job type.
    """

    def __init__(
        self,
        *,
        try_lock: AdvisoryTryLock,
        jobs: JobService,
        unit_of_work_factory: UnitOfWorkFactory,
        scans: Sequence[AnnotationScan],
    ) -> None:
        self._try_lock = try_lock
        self._jobs = jobs
        self._unit_of_work_factory = unit_of_work_factory
        self._scans: dict[JobType, AnnotationScan] = {}
        for scan in scans:
            if scan.job_type in self._scans:
                raise ValueError("job type claimed by more than one annotation scan")
            self._scans[scan.job_type] = scan

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return the job types this runner runs."""
        return frozenset(self._scans)

    def run(self, job_id: UUID) -> None:
        """Run one annotation scan job until it succeeds or fails.

        Nothing happens if another process is running the job or the job has
        finished. A job of a type no scan claims, or with invalid parameters,
        fails with `invalid_parameters`. A job whose scan finds required
        reference data missing fails with `reference_data_unavailable`.

        Args:
            job_id: The job to run.

        Raises:
            AnnotationScanBusyError: If another job of the same type, covering
                the same groups, is running. The job stays queued.
            Exception: Any other error from the scan. The job stays running, and
                the caller can retry the whole run.
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
            scan = self._scans.get(job.job_type)
            try:
                group_key = scan_groups(job.parameters)
            except ValueError:
                self._fail_invalid(job, scan)
                return
            if scan is None:
                self._fail_invalid(job, None)
                return
            lock_value = scan_lock_value(job.job_type, group_key)
            with self._try_lock(LockNamespace.ANNOTATION_SCAN, lock_value) as free:
                if not free:
                    raise AnnotationScanBusyError
                started = self._start(scan, job)
                if started is None:
                    return
                try:
                    outcome = scan.execute(started, group_key, self._reporter(job_id))
                except ReferenceDataUnavailableError:
                    self._fail(scan, started, JobFailureCode.REFERENCE_DATA_UNAVAILABLE)
                    return
                self._succeed(scan, started, outcome)

    def _start(self, scan: AnnotationScan, job: Job) -> Job | None:
        """Start the job, fail other running jobs like it, and run `prepare`.

        Other running jobs of the same type covering the same groups are failed
        with `abandoned`. A job that finishes while this one is starting keeps
        its outcome: finding the running jobs waits for it to commit. All of it
        commits in one transaction.

        Returns:
            The started job, or `None`, with nothing committed, if the job is no
            longer queued or running. That happens when the job was failed, for
            example with `dispatch_failed`, after its message reached the broker.
        """
        with self._unit_of_work_factory() as unit_of_work:
            started = self._jobs.record_start(unit_of_work, job.job_id)
            if started.status is not JobStatus.RUNNING:
                return None
            for record in unit_of_work.jobs.running(
                job_type=job.job_type,
                parameters=dict(job.parameters),
                excluding=job.job_id,
            ):
                abandoned = self._jobs.record_failure(
                    unit_of_work,
                    record.job_id,
                    error=scan.failure_message,
                    failure_code=JobFailureCode.ABANDONED,
                )
                scan.cleanup(unit_of_work, abandoned)
                logger.warning(
                    "Annotation scan job abandoned: job_id=%s job_type=%s found_by=%s",
                    abandoned.job_id,
                    abandoned.job_type.value,
                    job.job_id,
                )
            scan.prepare(unit_of_work, started)
            unit_of_work.commit()
        return started

    def _succeed(self, scan: AnnotationScan, job: Job, outcome: ScanOutcome) -> None:
        """Record success, final progress, and the scan's final work together."""
        with self._unit_of_work_factory() as unit_of_work:
            self._jobs.record_success(
                unit_of_work,
                job.job_id,
                result=outcome.result,
                progress={**outcome.counts, "phase": "completed"},
            )
            scan.finalize(unit_of_work, job)
            unit_of_work.commit()

    def _fail(self, scan: AnnotationScan, job: Job, code: JobFailureCode) -> None:
        """Fail a started job and remove what it stored, in one transaction."""
        logger.error(
            "Annotation scan job failed: job_id=%s job_type=%s failure_code=%s",
            job.job_id,
            job.job_type.value,
            code.value,
        )
        self._jobs.fail(
            job.job_id,
            error=scan.failure_message,
            failure_code=code,
            cleanup=lambda unit_of_work: scan.cleanup(unit_of_work, job),
        )

    def _fail_invalid(self, job: Job, scan: AnnotationScan | None) -> None:
        """Fail a job whose type or parameters this runner cannot run."""
        # Parameter values are not logged; they are untrusted stored data.
        logger.error(
            "Annotation scan job cannot run: job_id=%s job_type=%s failure_code=%s",
            job.job_id,
            job.job_type.value,
            JobFailureCode.INVALID_PARAMETERS.value,
        )
        self._jobs.fail(
            job.job_id,
            error=_GENERIC_FAILURE_MESSAGE if scan is None else scan.failure_message,
            failure_code=JobFailureCode.INVALID_PARAMETERS,
        )

    def _reporter(self, job_id: UUID) -> ProgressReporter:
        """Return a reporter that stores progress in its own transactions."""

        def report(
            phase: str,
            counts: Mapping[str, int] | None = None,
            warnings: tuple[str, ...] = (),
        ) -> None:
            self._jobs.update_progress(
                job_id, progress={"phase": phase, **(counts or {})}, warnings=warnings
            )

        return report
