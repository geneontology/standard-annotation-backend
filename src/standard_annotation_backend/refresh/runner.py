"""Run refresh and retirement jobs through one shared lifecycle.

Celery's `sab.refresh.run` task and the refresh CLI both call `RefreshRunner`,
so a job behaves the same whichever process runs it.

Celery can deliver one task message more than once. SAB acknowledges messages
only after a task finishes (`task_acks_late`), so if a worker stops mid-task the
broker delivers the message again, possibly to another worker. Every step below
is therefore safe to repeat: a finished job is left alone, work an earlier
attempt committed is recovered instead of redone, and only unfinished work runs
again.
"""

import logging
from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID

from sqlalchemy import Engine

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    UnknownSourceError,
    job_source_key,
    refresh_failure_message,
)
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    try_advisory_lock,
)
from standard_annotation_backend.refresh.fetchers import SourceError, SourceFetcher
from standard_annotation_backend.refresh.kinds import (
    RefreshKind,
    RefreshResult,
    RefreshRetirer,
    TerminalRefreshError,
)
from standard_annotation_backend.services.job_service import Job, JobService

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})


class RefreshRunner:
    """Run refresh jobs of every kind, and entity retirement jobs.

    Args:
        engine: Engine used for the job execution lock's dedicated connection.
        jobs: Reads and updates job state.
        fetchers: Fetches configured sources.
        kinds: Every refresh kind this runner can run.
        retirer: Runs retirement jobs.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        jobs: JobService,
        fetchers: SourceFetcher,
        kinds: Sequence[RefreshKind],
        retirer: RefreshRetirer,
    ) -> None:
        self._engine = engine
        self._jobs = jobs
        self._fetchers = fetchers
        # Jobs name their kind only through their job type, so kinds are
        # looked up by the job type of their refresh jobs.
        self._kinds = {kind.job_type: kind for kind in kinds}
        self._retirer = retirer

    def run(self, job_id: UUID) -> None:
        """Run one refresh job until it succeeds or fails.

        Known failures (retrieval errors, unknown sources, and each kind's
        terminal errors) fail the job with a `failure_code`.

        Raises:
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
        with try_advisory_lock(self._engine, LockNamespace.JOB, job_id) as acquired:
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
        with try_advisory_lock(self._engine, LockNamespace.JOB, job_id) as acquired:
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
                    # The job is a retirement job with bad parameters. The
                    # retirer fails its own jobs, so the error names the entity
                    # kind and its staging cleanup runs.
                    self._retirer.fail(
                        job.job_id, RefreshFailureCode.INVALID_PARAMETERS, None
                    )
                    return
                # Any other job type belongs to no kind this method serves, so
                # no kind can name it or clean up after it. Fail it generically.
                self._jobs.fail_refresh(
                    job.job_id,
                    error=refresh_failure_message(None),
                    failure_code=RefreshFailureCode.INVALID_PARAMETERS,
                )
                return
            # Retirement commits with its audit event. If this process stops
            # before `succeed`, a redelivery calls `retire` again, which returns
            # the stored result without retiring twice.
            self._finish(job, self._retirer.retire(job, source_key))

    def _run(self, kind: RefreshKind, job: Job, source_key: str) -> None:
        """Run a started job and record whether it succeeded or failed."""
        try:
            result = self._execute(kind, job, source_key)
        except Exception as error:
            classified = _classify(kind, error)
            if classified is None:
                # Not a known failure: leave the job running and let the caller
                # retry. Committed work is recovered on the next attempt.
                raise
            failure_code, failure_details = classified
            # Log the exception type and code only. Messages and details can
            # contain source URLs or rejected values.
            logger.error(
                "Refresh job failed: job_id=%s kind=%s failure_type=%s failure_code=%s",
                job.job_id,
                kind.name.value,
                type(error).__name__,
                failure_code.value,
            )
            # If recording the failure itself fails (for example the database
            # connection drops), that error propagates and the job stays
            # running, so a retry classifies the failure again.
            kind.fail(job.job_id, failure_code, failure_details)
            kind.after_terminal(source_key)
            return
        self._finish(job, result)
        kind.after_terminal(source_key)

    def _execute(self, kind: RefreshKind, job: Job, source_key: str) -> RefreshResult:
        """Recover, or fetch and apply, and return the result to store."""
        # An earlier attempt may have committed its work and then stopped before
        # marking the job succeeded. Finish from that work without fetching.
        recovered = kind.recover(job)
        if recovered is not None:
            return recovered
        # Look the source up now rather than when the job was created: the
        # sources file may have changed since, and a removed source must not be
        # fetched.
        source = kind.sources().get(source_key)
        if source is None:
            raise UnknownSourceError(kind.name, source_key)
        self._jobs.update_progress(job.job_id, progress={"phase": "fetching"})
        document = self._fetchers.fetch(source_key, source)
        # Skip applying a document that is already active.
        unchanged = kind.unchanged(source_key, document)
        if unchanged is not None:
            # Every kind reports "nothing applied because this document is
            # already active" the same way, with `progress["unchanged"]`. Kinds
            # keep their own result shapes, so the flag is added here.
            return replace(
                unchanged, progress={**unchanged.progress, "unchanged": True}
            )
        self._jobs.update_progress(job.job_id, progress={"phase": "applying"})

        def report(progress: dict[str, object], warnings: tuple[str, ...] = ()) -> None:
            self._jobs.update_progress(job.job_id, progress=progress, warnings=warnings)

        return kind.apply(job, document, report)

    def _finish(self, job: Job, result: RefreshResult) -> None:
        """Store final progress, then mark the job succeeded with its result."""
        # These are two transactions. If the process stops between them, the
        # job stays running and the next delivery recovers the same result.
        self._jobs.update_progress(
            job.job_id,
            progress={**result.progress, "phase": "completed"},
            warnings=result.warnings,
        )
        self._jobs.succeed(job.job_id, result=result.result)

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
            # The kind fails its own jobs, so its cleanup (such as deleting
            # staging) runs and the error names the kind.
            kind.fail(job.job_id, RefreshFailureCode.INVALID_PARAMETERS, None)
            return
        self._jobs.fail_refresh(
            job.job_id,
            error=refresh_failure_message(None),
            failure_code=RefreshFailureCode.INVALID_PARAMETERS,
        )


def _source_key_or_none(job: Job) -> str | None:
    """Return the job's validated source key, or `None` if its parameters are bad."""
    try:
        return job_source_key(job.parameters)
    except ValueError:
        return None


def _classify(
    kind: RefreshKind, error: Exception
) -> tuple[RefreshFailureCode, dict[str, object] | None] | None:
    """Return the failure code and details for an error retrying cannot fix.

    Source and parsing failures are in this group: retrying the same job would
    fetch the same broken or unavailable document, and the next scheduled
    refresh tries again anyway. Returns `None` for any other error, which the
    caller re-raises so the run is retried.
    """
    if isinstance(error, SourceError):
        return error.code, None
    if isinstance(error, UnknownSourceError):
        return RefreshFailureCode.UNKNOWN_SOURCE, None
    if isinstance(error, TerminalRefreshError):
        return error.failure_code, error.failure_details
    for error_type, failure_code in kind.terminal_errors.items():
        if isinstance(error, error_type):
            return failure_code, None
    return None
