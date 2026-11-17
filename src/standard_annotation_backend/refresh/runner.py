"""Run refresh jobs through one shared lifecycle.

Celery's `sabe.job.run` task and the refresh CLI both reach `RefreshRunner` through
`JobExecutors`, so a job behaves the same whichever process runs it.

A refresh job is one transaction. The runner fetches the source, then opens a
unit of work, calls the kind's `apply`, records the job's success with its
result and final progress, and commits once. What a refresh stores and the
record that it succeeded therefore commit together: a job that stops partway
leaves nothing behind and runs again from the start. Jobs that `apply` queued,
such as a term replacement job after an ontology activation, are sent to workers
after the commit.

Celery can deliver one task message more than once. SABE acknowledges messages
only after a task finishes (`task_acks_late`), so if a worker stops mid-task the
broker delivers the message again, possibly to another worker. A finished job is
left alone, and an unfinished one runs again from the start.

A refresh kind, described by `RefreshKind`, supplies only `apply`. A
`TerminalRefreshError` from it fails the job with its failure code; any other
exception propagates so Celery can retry. While `apply` runs, progress is stored
in separate transactions, so clients can follow a long job.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol
from uuid import UUID

from standard_annotation_backend.domain.jobs import (
    TERMINAL_JOB_STATUSES,
    FollowUpJob,
    JobFailureCode,
    JobType,
    ProgressReporter,
)
from standard_annotation_backend.domain.refresh import (
    RefreshKindName,
    RefreshOutcome,
    SourceDocument,
    TerminalRefreshError,
    UnknownSourceError,
    job_source_key,
    refresh_failure_message,
)
from standard_annotation_backend.persistence.locks import (
    AdvisoryTryLock,
    LockNamespace,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.refresh.fetchers import SourceFetcher
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.job_service import Job, JobService

logger = logging.getLogger(__name__)


class RefreshKind(Protocol):
    """Supply the kind-specific step of a refresh.

    `RefreshRunner.run` fetches the source and calls `apply` inside the job's
    transaction.
    """

    @property
    def name(self) -> RefreshKindName:
        """Return the kind's name."""
        ...

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return the job types of this kind's refresh jobs.

        No two kinds given to one `RefreshRunner` may share a job type.
        """
        ...

    def apply(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        job: Job,
        document: SourceDocument,
        report: ProgressReporter,
    ) -> RefreshOutcome:
        """Apply `document` in `unit_of_work`, or recognize that it is already active.

        `apply` never commits; the runner commits once the job's success is
        recorded in the same transaction.

        Returns:
            What was applied. `unchanged` is `True` when nothing was applied
            because `document` was already active.
        """
        ...


class RefreshRunner:
    """Run refresh jobs of every kind.

    Args:
        try_lock: Takes the job execution lock.
        jobs: Reads and updates job state.
        unit_of_work_factory: Opens each refresh job's transaction.
        fetchers: Fetches configured sources.
        sources: Configured sources for every kind, looked up when a job runs.
        kinds: Every refresh kind this runner can run.
        dispatch_follow_up: Sends a job that a refresh queued to be run, after
            the refresh commits. `None` leaves such jobs queued.

    Raises:
        ValueError: If two kinds claim the same job type.
    """

    def __init__(
        self,
        *,
        try_lock: AdvisoryTryLock,
        jobs: JobService,
        unit_of_work_factory: UnitOfWorkFactory,
        fetchers: SourceFetcher,
        sources: RefreshSources,
        kinds: Sequence[RefreshKind],
        dispatch_follow_up: Callable[[Job], None] | None = None,
    ) -> None:
        self._try_lock = try_lock
        self._jobs = jobs
        self._unit_of_work_factory = unit_of_work_factory
        self._fetchers = fetchers
        self._sources = sources
        # Jobs name their kind only through their job type, so kinds are
        # looked up by the job types of their refresh jobs. A job type claimed
        # twice would make the choice of kind arbitrary, so it is rejected.
        self._kinds: dict[JobType, RefreshKind] = {}
        for kind in kinds:
            for job_type in kind.job_types:
                if job_type in self._kinds:
                    raise ValueError("job type claimed by more than one refresh kind")
                self._kinds[job_type] = kind
        self._dispatch_follow_up = dispatch_follow_up

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return the job types of every refresh kind this runner runs."""
        return frozenset(self._kinds)

    def run(self, job_id: UUID) -> None:
        """Run one refresh job until it succeeds or fails.

        An error that subclasses `TerminalRefreshError` fails the job with its
        `failure_code`.

        Raises:
            Exception: Any other error, such as a lost database connection. The
                job's transaction rolls back and the job stays running, so the
                caller can retry the whole run.
        """
        # The job execution lock is a PostgreSQL try-lock held on a dedicated
        # connection for this whole run. If another process already runs this
        # job (for example after a duplicate delivery), return immediately. If
        # this process dies, PostgreSQL releases the lock with the connection,
        # and Celery redelivers the unacknowledged task message (see
        # `task_acks_late` and `task_reject_on_worker_lost` at
        # https://docs.celeryq.dev/en/stable/userguide/configuration.html), so
        # a later run starts the same `running` job again.
        with self._try_lock(LockNamespace.JOB, job_id) as acquired:
            if not acquired:
                return
            # `start` moves a queued job to running and adds an audit entry. For
            # a job that is already running, succeeded, or failed it returns the
            # state as is.
            job = self._jobs.start(job_id)
            if job.status in TERMINAL_JOB_STATUSES:
                return
            kind = self._kinds.get(job.job_type)
            source_key = _source_key_or_none(job)
            if kind is None or source_key is None:
                # Retrying cannot fix a job this runner does not understand, so
                # it fails now instead of being retried forever.
                self._fail_invalid(job, kind)
                return
            self._run(kind, job, source_key)

    def _run(self, kind: RefreshKind, job: Job, source_key: str) -> None:
        """Run a started job, record its success or failure, and send follow-ups."""
        try:
            follow_ups = self._execute(kind, job, source_key)
        except TerminalRefreshError as error:
            # Log the exception type and code only. Messages and details can
            # contain source URLs or rejected values.
            logger.error(
                "Refresh job failed: job_id=%s kind=%s failure_type=%s failure_code=%s",
                job.job_id,
                kind.name.value,
                type(error).__name__,
                error.failure_code.value,
            )
            # If recording the failure itself fails, that error propagates and
            # the job stays running, so a retry classifies the failure again.
            self._fail(job.job_id, kind.name, error.failure_code, error.failure_details)
            return
        for follow_up in follow_ups:
            self._send_follow_up(follow_up)

    def _execute(
        self, kind: RefreshKind, job: Job, source_key: str
    ) -> tuple[FollowUpJob, ...]:
        """Fetch, apply, and record success in one transaction.

        Returns:
            The jobs `apply` queued, to send once the transaction has committed.
        """
        # Look the source up now rather than when the job was created: the
        # sources file may have changed since, and a removed source must not be
        # fetched.
        source = self._sources.sources(kind.name).get(source_key)
        if source is None:
            raise UnknownSourceError(kind.name, source_key)
        self._report(job.job_id, "fetching")
        document = self._fetchers.fetch(source_key, source)
        self._report(job.job_id, "applying")

        def report(
            phase: str,
            counts: Mapping[str, int] | None = None,
            warnings: tuple[str, ...] = (),
        ) -> None:
            self._report(job.job_id, phase, counts, warnings)

        with self._unit_of_work_factory() as unit_of_work:
            outcome = kind.apply(unit_of_work, job, document, report)
            self._jobs.record_success(
                unit_of_work,
                job.job_id,
                result={**outcome.result, "unchanged": outcome.unchanged},
                progress={
                    **outcome.counts,
                    "phase": "completed",
                    "unchanged": outcome.unchanged,
                },
                warnings=outcome.warnings,
            )
            unit_of_work.commit()
        return outcome.follow_ups

    def _send_follow_up(self, follow_up: FollowUpJob) -> None:
        """Send a queued follow-up job after the refresh has committed.

        A job the refresh created is failed with `dispatch_failed` if sending it
        fails. If even that cannot be recorded, the error is logged and the job
        stays queued until the next refresh that queues the same job joins it
        and sends it. Nothing here can fail the refresh, which has already
        succeeded.
        """
        if self._dispatch_follow_up is None:
            return
        try:
            self._jobs.send(
                self._jobs.find(follow_up.job_id),
                created=follow_up.created,
                dispatch=self._dispatch_follow_up,
            )
        except Exception as error:
            logger.error(
                "Follow-up job could not be sent: job_id=%s failure_type=%s",
                follow_up.job_id,
                type(error).__name__,
            )

    def _report(
        self,
        job_id: UUID,
        phase: str,
        counts: Mapping[str, int] | None = None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        """Store a running job's progress and warnings in their own transaction."""
        self._jobs.update_progress(
            job_id, progress={"phase": phase, **(counts or {})}, warnings=warnings
        )

    def _fail_invalid(self, job: Job, kind: RefreshKind | None) -> None:
        """Fail a job whose type or parameters this runner cannot run."""
        # Parameter values are not logged; they are untrusted stored data.
        logger.error(
            "Refresh job cannot run: job_id=%s job_type=%s failure_code=%s",
            job.job_id,
            job.job_type.value,
            JobFailureCode.INVALID_PARAMETERS.value,
        )
        self._fail(
            job.job_id,
            None if kind is None else kind.name,
            JobFailureCode.INVALID_PARAMETERS,
            None,
        )

    def _fail(
        self,
        job_id: UUID,
        kind_name: RefreshKindName | None,
        failure_code: JobFailureCode,
        failure_details: dict[str, object] | None,
    ) -> None:
        """Mark a job failed and audit it in one transaction.

        Args:
            job_id: The job to fail.
            kind_name: The kind named in the stored error, or `None` if no kind
                owns the job.
            failure_code: Machine-readable reason stored on the job.
            failure_details: Optional JSON-compatible details stored with the
                failure.
        """
        self._jobs.fail(
            job_id,
            error=refresh_failure_message(kind_name),
            failure_code=failure_code,
            failure_details=failure_details,
        )


def _source_key_or_none(job: Job) -> str | None:
    """Return the job's validated source key, or `None` if its parameters are bad."""
    try:
        return job_source_key(job.parameters)
    except ValueError:
        return None
