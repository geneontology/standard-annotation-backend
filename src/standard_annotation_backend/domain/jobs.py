"""Define supported asynchronous job types, lifecycle states, and job errors."""

from collections.abc import Mapping
from enum import StrEnum
from uuid import UUID

from standard_annotation_backend.domain.errors import NotFoundError


class JobType(StrEnum):
    """List asynchronous operations that workers can execute."""

    ANNOTATION_CUTOVER = "annotation_cutover"
    ANNOTATION_QC_REPORT = "annotation_qc_report"
    ANNOTATION_REFRESH = "annotation_refresh"
    AUTHORIZATION_REFRESH = "authorization_refresh"
    ENTITY_REFRESH = "entity_refresh"
    ENTITY_RETIREMENT = "entity_retirement"
    ONTOLOGY_REFRESH = "ontology_refresh"
    RULES_REFRESH = "rules_refresh"
    TERM_REPLACEMENT = "term_replacement"


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


def group_scope_parameters(group_key: str | None) -> dict[str, object]:
    """Return the parameters of a job for one owning group, or for all groups."""
    return {} if group_key is None else {"group_key": group_key}


def group_scope(parameters: Mapping[str, object]) -> str | None:
    """Return the owning group a job's parameters name, or `None` for all groups.

    Term replacement and QC report jobs are scoped this way.

    Raises:
        ValueError: If the parameters are neither `{}` nor `{"group_key": key}`
            with a non-blank key and no surrounding whitespace.
    """
    if not parameters:
        return None
    group_key = parameters.get("group_key")
    if (
        set(parameters) != {"group_key"}
        or not isinstance(group_key, str)
        or not group_key
        or group_key != group_key.strip()
    ):
        raise ValueError("job scope parameters must be {} or {group_key}")
    return group_key


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
