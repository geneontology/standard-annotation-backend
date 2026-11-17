"""Verify the errors stored when a job cannot be sent to a worker."""

import pytest

from standard_annotation_backend.domain.jobs import JobType, dispatch_failure_message


@pytest.mark.parametrize("job_type", list(JobType))
def test_every_job_type_has_a_dispatch_failure_message(job_type: JobType) -> None:
    """Every job type has an error to store when a new job of it cannot be sent.

    Without one, a failed send of that type would raise instead of failing the job.
    """
    assert dispatch_failure_message(job_type).endswith(" could not be dispatched")
