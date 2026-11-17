"""Verify that every job type is run by exactly one runner."""

from typing import NoReturn
from uuid import UUID

import pytest

from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.services.job_executors import JobExecutors
from standard_annotation_backend.services.job_service import JobService


class _Runner:
    """Claim job types without running anything."""

    def __init__(self, *job_types: JobType) -> None:
        self._job_types = frozenset(job_types)

    @property
    def job_types(self) -> frozenset[JobType]:
        return self._job_types

    def run(self, job_id: UUID) -> None:
        raise AssertionError("not run in these tests")


def _no_database() -> NoReturn:
    raise AssertionError("these tests open no transaction")


JOBS = JobService(_no_database)


def test_a_job_type_claimed_twice_is_rejected() -> None:
    """Each job type has one runner, so ambiguity fails at startup."""
    with pytest.raises(ValueError, match="more than one runner"):
        JobExecutors(JOBS, (_Runner(*JobType), _Runner(JobType.TERM_REPLACEMENT)))


def test_a_job_type_without_a_runner_is_rejected() -> None:
    """A new job type cannot be added without saying what runs it."""
    claimed = [
        job_type for job_type in JobType if job_type is not JobType.TERM_REPLACEMENT
    ]

    with pytest.raises(ValueError, match="term_replacement"):
        JobExecutors(JOBS, (_Runner(*claimed),))
