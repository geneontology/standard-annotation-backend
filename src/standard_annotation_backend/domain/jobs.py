"""Define supported asynchronous job types and lifecycle states."""

from enum import StrEnum


class JobType(StrEnum):
    """List asynchronous operations that workers can execute."""

    AUTHORIZATION_SYNC = "authorization_sync"
    ENTITY_CATALOG_RETIREMENT = "entity_catalog_retirement"
    ENTITY_IMPORT = "entity_import"
    ONTOLOGY_LOAD = "ontology_load"


class JobStatus(StrEnum):
    """List the states stored during an asynchronous job's lifecycle."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
