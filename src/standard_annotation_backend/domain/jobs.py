"""Define supported asynchronous job types, lifecycle states, and job errors."""

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from standard_annotation_backend.domain.errors import NotFoundError


class JobType(StrEnum):
    """List asynchronous operations that workers can execute."""

    ANNOTATION_CUTOVER = "annotation_cutover"
    ANNOTATION_QC_REPORT = "annotation_qc_report"
    ANNOTATION_REFRESH = "annotation_refresh"
    AUTHORIZATION_REFRESH = "authorization_refresh"
    ENTITY_REFRESH = "entity_refresh"
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


class JobFailureCode(StrEnum):
    """List the codes recorded when a job fails in a way retrying cannot fix.

    The codes are fixed strings, so job records and logs never contain source URLs
    or content. Refresh jobs use every code except `reference_data_unavailable`
    and `abandoned`; operators use them to tell retrieval, decoding, parsing, and
    activation or publication failures apart. Annotation scan jobs (term
    replacement and QC reports) use `invalid_parameters`, `dispatch_failed`, and
    `reference_data_unavailable`, the last when a configured ontology or the
    go-site rule data has no active snapshot. Annotation scan jobs also use
    `abandoned`: the job was still running, but no process was running it, when
    a later job of the same type covering the same groups started. Every job
    type can fail with
    `dispatch_failed` when it was created but could not be sent to a worker.
    """

    INVALID_PARAMETERS = "invalid_parameters"
    DISPATCH_FAILED = "dispatch_failed"
    UNKNOWN_SOURCE = "unknown_source"
    SOURCE_ERROR = "source_error"
    HTTP_STATUS = "http_status"
    TIMEOUT = "timeout"
    INVALID_GZIP = "invalid_gzip"
    INVALID_UTF8 = "invalid_utf8"
    INVALID_DOCUMENT = "invalid_document"
    HEADER = "header"
    NO_VALID_ROWS = "no_valid_rows"
    CUTOVER_REJECTED = "cutover_rejected"
    GROUP_SABE_MANAGED = "group_sabe_managed"
    REFERENCE_DATA_UNAVAILABLE = "reference_data_unavailable"
    ABANDONED = "abandoned"


class ProgressReporter(Protocol):
    """Store a running job's phase, counts, and warnings as its progress.

    Each runner passes one to the code that does a job's work, so that code
    never builds progress dictionaries itself. Each call replaces the job's
    stored progress and warnings, in a transaction of its own, so clients can
    follow a long job.
    """

    def __call__(
        self,
        phase: str,
        counts: Mapping[str, int] | None = None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        """Store `phase`, `counts`, and `warnings` on the job.

        Args:
            phase: The step the job is in, such as `checking`.
            counts: Counts to store with the phase, if any.
            warnings: Short messages to store on the job.
        """
        ...


_DISPATCH_FAILURE_MESSAGES: dict[JobType, str] = {
    JobType.AUTHORIZATION_REFRESH: "Authorization refresh could not be dispatched",
    JobType.RULES_REFRESH: "Rules refresh could not be dispatched",
    JobType.ONTOLOGY_REFRESH: "Ontology refresh could not be dispatched",
    JobType.ENTITY_REFRESH: "Entity refresh could not be dispatched",
    JobType.ANNOTATION_REFRESH: "Annotation refresh could not be dispatched",
    JobType.ANNOTATION_CUTOVER: "Annotation refresh could not be dispatched",
    JobType.TERM_REPLACEMENT: "Term replacement could not be dispatched",
    JobType.ANNOTATION_QC_REPORT: "QC report could not be dispatched",
}


def dispatch_failure_message(job_type: JobType) -> str:
    """Return the public error stored when a new job of this type cannot be sent."""
    return _DISPATCH_FAILURE_MESSAGES[job_type]


def scan_group_parameters(group_key: str | None) -> dict[str, object]:
    """Return job parameters for one owning group, or for all groups when `None`."""
    return {} if group_key is None else {"group_key": group_key}


def scan_groups(parameters: Mapping[str, object]) -> str | None:
    """Return the owning group a job's parameters name, or `None` for all groups.

    Annotation scan jobs (term replacement and QC reports) store the groups they
    cover in this form, which `scan_group_parameters` writes.

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
        raise ValueError("scan group parameters must be {} or {group_key}")
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
