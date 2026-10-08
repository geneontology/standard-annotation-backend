"""Define supported asynchronous job types, lifecycle states, and job errors."""

from enum import StrEnum
from uuid import UUID

from standard_annotation_backend.domain.errors import NotFoundError


class JobType(StrEnum):
    """List asynchronous operations that workers can execute."""

    ANNOTATION_CUTOVER = "annotation_cutover"
    ANNOTATION_REFRESH = "annotation_refresh"
    AUTHORIZATION_REFRESH = "authorization_refresh"
    ENTITY_REFRESH = "entity_refresh"
    ENTITY_RETIREMENT = "entity_retirement"
    ONTOLOGY_REFRESH = "ontology_refresh"


class JobStatus(StrEnum):
    """List the states stored during an asynchronous job's lifecycle."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ACTIVE_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})
"""States of a job that has not finished and may still change."""

TERMINAL_JOB_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})
"""States of a finished job, which never changes state again."""


class JobNotFoundError(NotFoundError):
    """Report an operation that targets an unknown job.

    Attributes:
        job_id: Identifier of the job that was not found.
    """

    code = "job_not_found"
    message = "Job was not found"

    def __init__(self, job_id: UUID) -> None:
        self.job_id = job_id
        super().__init__()
