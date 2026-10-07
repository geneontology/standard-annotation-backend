"""Test the classification of job lifecycle states and job lookup errors."""

from uuid import uuid4

from fastapi import status

from standard_annotation_backend.api.errors import status_for
from standard_annotation_backend.domain.jobs import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    JobNotFoundError,
    JobStatus,
)


def test_every_job_status_is_either_active_or_terminal() -> None:
    """Each lifecycle state is classified as exactly one of active or terminal."""
    assert ACTIVE_JOB_STATUSES.isdisjoint(TERMINAL_JOB_STATUSES)
    assert frozenset(JobStatus) == ACTIVE_JOB_STATUSES | TERMINAL_JOB_STATUSES
    assert {JobStatus.QUEUED, JobStatus.RUNNING} == ACTIVE_JOB_STATUSES


def test_job_not_found_is_a_not_found_error() -> None:
    """An unknown job is reported as `job_not_found` and keeps its identifier."""
    job_id = uuid4()
    error = JobNotFoundError(job_id)

    assert status_for(type(error)) == status.HTTP_404_NOT_FOUND
    assert error.code == "job_not_found"
    assert error.message == "Job was not found"
    assert error.job_id == job_id
