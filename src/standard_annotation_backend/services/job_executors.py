"""Map every job type to the runner that executes it.

Celery's `sabe.job.run` task and the refresh CLI both run jobs through
`JobExecutors`, so a job runs the same way whichever process runs it.
`RefreshRunner` runs refresh jobs and `AnnotationScanRunner` runs annotation scan
jobs (term replacement and QC reports).
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from standard_annotation_backend.domain.jobs import JobNotFoundError, JobType
from standard_annotation_backend.services.job_service import JobService


class JobRunner(Protocol):
    """Run jobs of the job types it claims."""

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return the job types this runner runs."""
        ...

    def run(self, job_id: UUID) -> None:
        """Run one job until it succeeds or fails."""
        ...


class JobExecutors:
    """Run any job with the runner for its type.

    Args:
        jobs: Reads a job's type.
        runners: The runners, which together claim every job type once.

    Raises:
        ValueError: If two runners claim one job type, or no runner claims a job
            type.
    """

    def __init__(self, jobs: JobService, runners: Sequence[JobRunner]) -> None:
        self._jobs = jobs
        self._runners: dict[JobType, JobRunner] = {}
        for runner in runners:
            for job_type in runner.job_types:
                if job_type in self._runners:
                    raise ValueError("job type claimed by more than one runner")
                self._runners[job_type] = runner
        missing = sorted(
            job_type.value for job_type in set(JobType) - set(self._runners)
        )
        if missing:
            raise ValueError(f"no runner for job types: {', '.join(missing)}")

    def run(self, job_id: UUID) -> None:
        """Run one job with its type's runner; ignore a job that does not exist.

        Args:
            job_id: The job to run.

        Raises:
            Exception: Whatever the runner raises, such as a busy error that asks
                the caller to retry later.
        """
        try:
            job = self._jobs.find(job_id)
        except JobNotFoundError:
            return
        self._runners[job.job_type].run(job_id)
