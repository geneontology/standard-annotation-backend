"""Test the classification of job lifecycle states."""

from standard_annotation_backend.domain.jobs import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    JobStatus,
)


def test_every_job_status_is_either_active_or_terminal() -> None:
    """Each lifecycle state is classified as exactly one of active or terminal."""
    assert ACTIVE_JOB_STATUSES.isdisjoint(TERMINAL_JOB_STATUSES)
    assert frozenset(JobStatus) == ACTIVE_JOB_STATUSES | TERMINAL_JOB_STATUSES
    assert {JobStatus.QUEUED, JobStatus.RUNNING} == ACTIVE_JOB_STATUSES
