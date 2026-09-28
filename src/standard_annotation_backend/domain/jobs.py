"""Define supported asynchronous job types and lifecycle states."""

from enum import StrEnum


class JobType(StrEnum):
    """List asynchronous operations that workers can execute."""

    AUTHORIZATION_SYNC = "authorization_sync"


class JobStatus(StrEnum):
    """List the states stored during an asynchronous job's lifecycle."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
