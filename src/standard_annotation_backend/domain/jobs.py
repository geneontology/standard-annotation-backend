"""Define supported asynchronous job types and lifecycle states."""

from enum import StrEnum


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
