"""Run refresh and retirement jobs through one shared lifecycle.

Celery's `sab.refresh.run` task and the refresh CLI both call `RefreshRunner`,
so a job behaves the same whichever process runs it.

Celery can deliver one task message more than once. SAB acknowledges messages
only after a task finishes (`task_acks_late`), so if a worker stops mid-task the
broker delivers the message again, possibly to another worker. Every step below
is therefore safe to repeat: a finished job is left alone, work an earlier
attempt committed is recovered instead of redone, and only unfinished work runs
again.

The runner drives every job through the same steps. A refresh kind, described
by `RefreshKind`, supplies only what differs: how it recovers work a previous
attempt committed, how it applies a document (including recognizing one that is
already active), and what staging to discard when a job fails. Each refresh
service implements `RefreshKind` and returns a `RefreshOutcome`. The runner
alone builds the job's progress, records `unchanged`, and classifies errors: a
`TerminalRefreshError` fails the job, while a `RetryableRefreshError` or any
other exception propagates so Celery can retry. When a job fails, the runner
calls the service's `discard_staging` in the same transaction that records the
failure.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol
from uuid import UUID

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import (
    ProgressReporter,
    RefreshFailureCode,
    RefreshKindName,
    RefreshOutcome,
    RetryableRefreshError,
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
)
from standard_annotation_backend.refresh.fetchers import SourceFetcher
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.job_service import Job, JobService

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})


class RefreshKind(Protocol):
    """Supply the kind-specific steps of a refresh.

    `RefreshRunner.run` calls these methods in a fixed order for every job.
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

    def recover(self, job: Job) -> RefreshOutcome | None:
        """Return an outcome committed by an earlier attempt of this job, if any."""
        ...

    def apply(
        self, job: Job, document: SourceDocument, report: ProgressReporter
    ) -> RefreshOutcome:
        """Apply `document`, or recognize that it is already active.

        Returns:
            The committed outcome. `unchanged` is `True` when nothing was
            applied because `document` was already active.
        """
        ...

    def discard_staging(self, uow: SqlAlchemyUnitOfWork, job_id: UUID) -> None:
        """Delete the job's staging inside the failure transaction, if any."""
        ...

    def after_terminal(self, source_key: str) -> None:
        """Do follow-up work once a job has succeeded or failed."""
        ...


class RefreshRetirer(Protocol):
    """Retire data for a source that is no longer configured."""

    def retire(self, job: Job, source_key: str) -> RefreshOutcome:
        """Retire the source's active data and return the committed outcome."""
        ...

    def discard_staging(self, uow: SqlAlchemyUnitOfWork, job_id: UUID) -> None:
        """Delete the job's staging inside the failure transaction, if any."""
        ...


class RefreshRunner:
    """Run refresh jobs of every kind, and entity retirement jobs.

    Args:
        try_lock: Takes the job execution lock.
        jobs: Reads and updates job state.
        fetchers: Fetches configured sources.
        sources: Configured sources for every kind, looked up when a job runs.
        kinds: Every refresh kind this runner can run.
        retirer: Runs retirement jobs.

    Raises:
        ValueError: If two kinds claim the same job type.
    """

    def __init__(
        self,
        *,
        try_lock: AdvisoryTryLock,
        jobs: JobService,
        fetchers: SourceFetcher,
        sources: RefreshSources,
        kinds: Sequence[RefreshKind],
        retirer: RefreshRetirer,
    ) -> None:
        self._try_lock = try_lock
        self._jobs = jobs
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
        self._retirer = retirer

    def run(self, job_id: UUID) -> None:
        """Run one refresh job until it succeeds or fails.

        An error that subclasses `TerminalRefreshError` fails the job with its
        `failure_code`.

        Raises:
            RetryableRefreshError: If another process holds a lock this job
                needs. The job stays running so the caller can retry.
            Exception: Any other error, such as a lost database connection. The
                job and its committed work are left as they are, so the caller
                can retry the whole run.
        """
        # The job execution lock is a PostgreSQL try-lock held on a dedicated
        # connection for this whole run. If another process already runs this
        # job (for example after a duplicate delivery), return immediately. If
        # this process dies, PostgreSQL releases the lock with the connection,
        # and Celery redelivers the unacknowledged task message (see
        # `task_acks_late` and `task_reject_on_worker_lost` at
        # https://docs.celeryq.dev/en/stable/userguide/configuration.html), so
        # a later run resumes the same `running` job.
        with self._try_lock(LockNamespace.JOB, job_id) as acquired:
            if not acquired:
                return
            # `start` moves a queued job to running and adds an audit entry. For a
            # job that is already running, succeeded, or failed it returns the state
            # as is. A job that is already running was interrupted by a lost worker;
            # this run continues it.
            job = self._jobs.start(job_id)
            kind = self._kinds.get(job.job_type)
            source_key = _source_key_or_none(job)
            if job.status in _TERMINAL:
                # A redelivered message for a finished job only repeats the kind's
                # follow-up work, such as scheduling ontology pruning, which is
                # safe to repeat.
                if kind is not None and source_key is not None:
                    kind.after_terminal(source_key)
                return
            if kind is None or source_key is None:
                # Retrying cannot fix a job this runner does not understand, so
                # it fails now instead of being retried forever.
                self._fail_invalid(job, kind)
                return
            self._run(kind, job, source_key)

    def run_retirement(self, job_id: UUID) -> None:
        """Run one entity retirement job until it succeeds or fails.

        Raises:
            Exception: Any unexpected error, so the caller can retry.
        """
        # Same lock and redelivery handling as `run`.
        with self._try_lock(LockNamespace.JOB, job_id) as acquired:
            if not acquired:
                return
            job = self._jobs.start(job_id)
            if job.status in _TERMINAL:
                return
            source_key = _source_key_or_none(job)
            if job.job_type is not JobType.ENTITY_RETIREMENT or source_key is None:
                logger.error(
                    "Retirement job cannot run: job_id=%s job_type=%s failure_code=%s",
                    job.job_id,
                    job.job_type.value,
                    RefreshFailureCode.INVALID_PARAMETERS.value,
                )
                if job.job_type is JobType.ENTITY_RETIREMENT:
                    # The job is a retirement job with bad parameters, so the
                    # error names the entity kind and its staging is discarded.
                    self._fail(
                        job.job_id,
                        RefreshKindName.ENTITY,
                        RefreshFailureCode.INVALID_PARAMETERS,
                        None,
                        self._retirer.discard_staging,
                    )
                    return
                # Any other job type belongs to no kind this method serves, so
                # no kind can name it or clean up after it. Fail it generically.
                self._fail(
                    job.job_id,
                    None,
                    RefreshFailureCode.INVALID_PARAMETERS,
                    None,
                    None,
                )
                return
            # Retirement commits with its audit event. If this process stops
            # before `succeed`, a redelivery calls `retire` again, which returns
            # the stored result without retiring twice.
            self._finish(job, self._retirer.retire(job, source_key), refresh=False)

    def _run(self, kind: RefreshKind, job: Job, source_key: str) -> None:
        """Run a started job and record whether it succeeded or failed."""
        try:
            outcome = self._execute(kind, job, source_key)
        except RetryableRefreshError as error:
            # Expected contention: the job stays running and Celery retries.
            logger.info(
                "Refresh job waiting: job_id=%s kind=%s failure_type=%s",
                job.job_id,
                kind.name.value,
                type(error).__name__,
            )
            raise
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
            self._fail(
                job.job_id,
                kind.name,
                error.failure_code,
                error.failure_details,
                kind.discard_staging,
            )
            kind.after_terminal(source_key)
            return
        self._finish(job, outcome, refresh=True)
        kind.after_terminal(source_key)

    def _execute(self, kind: RefreshKind, job: Job, source_key: str) -> RefreshOutcome:
        """Recover, or fetch and apply, and return the committed outcome."""
        # An earlier attempt may have committed its work and then stopped before
        # marking the job succeeded. Finish from that work without fetching.
        recovered = kind.recover(job)
        if recovered is not None:
            return recovered
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

        return kind.apply(job, document, report)

    def _report(
        self,
        job_id: UUID,
        phase: str,
        counts: Mapping[str, int] | None = None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        """Store a running job's phase, counts, and warnings."""
        self._jobs.update_progress(
            job_id, progress={"phase": phase, **(counts or {})}, warnings=warnings
        )

    def _finish(self, job: Job, outcome: RefreshOutcome, *, refresh: bool) -> None:
        """Store final progress, then mark the job succeeded with its result.

        Refresh jobs always record whether the document was unchanged, in both
        progress and result. Retirement jobs have no document, so they do not.
        """
        progress: dict[str, object] = {**outcome.counts, "phase": "completed"}
        result = dict(outcome.result)
        if refresh:
            progress["unchanged"] = outcome.unchanged
            result["unchanged"] = outcome.unchanged
        # These are two transactions. If the process stops between them, the
        # job stays running and the next delivery recovers the same result.
        self._jobs.update_progress(
            job.job_id, progress=progress, warnings=outcome.warnings
        )
        self._jobs.succeed(job.job_id, result=result)

    def _fail_invalid(self, job: Job, kind: RefreshKind | None) -> None:
        """Fail a job whose type or parameters this runner cannot run."""
        # Parameter values are not logged; they are untrusted stored data.
        logger.error(
            "Refresh job cannot run: job_id=%s job_type=%s failure_code=%s",
            job.job_id,
            job.job_type.value,
            RefreshFailureCode.INVALID_PARAMETERS.value,
        )
        if kind is not None:
            # The error names the kind and its staging is discarded.
            self._fail(
                job.job_id,
                kind.name,
                RefreshFailureCode.INVALID_PARAMETERS,
                None,
                kind.discard_staging,
            )
            return
        self._fail(job.job_id, None, RefreshFailureCode.INVALID_PARAMETERS, None, None)

    def _fail(
        self,
        job_id: UUID,
        kind_name: RefreshKindName | None,
        failure_code: RefreshFailureCode,
        failure_details: dict[str, object] | None,
        discard: Callable[[SqlAlchemyUnitOfWork, UUID], None] | None,
    ) -> None:
        """Mark a job failed, audit it, and discard its staging in one transaction.

        Args:
            job_id: The job to fail.
            kind_name: The kind named in the stored error, or `None` if no kind
                owns the job.
            failure_code: Machine-readable reason stored on the job.
            failure_details: Optional JSON-compatible details stored with the
                failure.
            discard: Deletes the job's staging in the same transaction, or
                `None` if there is nothing to delete.
        """
        self._jobs.fail_refresh(
            job_id,
            error=refresh_failure_message(kind_name),
            failure_code=failure_code,
            failure_details=failure_details,
            cleanup=None if discard is None else lambda uow: discard(uow, job_id),
        )


def _source_key_or_none(job: Job) -> str | None:
    """Return the job's validated source key, or `None` if its parameters are bad."""
    try:
        return job_source_key(job.parameters)
    except ValueError:
        return None
