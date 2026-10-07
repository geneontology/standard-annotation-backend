"""Define the vocabulary shared by every reference data refresh.

SAB keeps reference data current from external sources: authorization from
go-site `users.yaml`, ontologies from OBO files, entities from GPI files, and
annotations from group GPAD files. Refreshing one source means fetching it and
applying it. These values are shared by every kind and need neither HTTP nor a
database.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from standard_annotation_backend.domain.errors import InvalidInputError
from standard_annotation_backend.domain.jobs import JobType

SOURCE_KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
"""Pattern for every configured source key, also enforced in SQL checks."""

AUTHORIZATION_SOURCE_KEY = "go-site"
"""Fixed key of the single authorization source."""


class RefreshKindName(StrEnum):
    """Name each kind of reference data that SAB refreshes."""

    AUTHORIZATION = "authorization"
    ONTOLOGY = "ontology"
    ENTITY = "entity"
    ANNOTATION = "annotation"


class RefreshFailureCode(StrEnum):
    """List the codes recorded when a refresh job fails in a way retrying cannot fix.

    The codes are fixed strings, so job records and logs never contain source
    URLs or content. Operators use them to tell retrieval, decoding, parsing,
    and publication failures apart.
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
    ROW_VALIDATION = "row_validation"
    CANDIDATE_CONFLICT = "candidate_conflict"
    CATALOG_COLLISION = "catalog_collision"
    NO_VALID_ANNOTATIONS = "no_valid_annotations"
    CUTOVER_REJECTED = "cutover_rejected"
    GROUP_SAB_MANAGED = "group_sab_managed"


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Identify the exact source document that a snapshot was built from.

    Attributes:
        source_type: `github` or `https`.
        source_locator: `github:<repository>:<path>`, or the configured URL.
        source_revision: Resolved commit SHA for GitHub sources, otherwise `None`.
        source_checksum: Lowercase SHA-256 of the downloaded bytes, before any
            decompression.
        fetched_at: When retrieval completed.
    """

    source_type: str
    source_locator: str
    source_revision: str | None
    source_checksum: str
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """Contain fetched bytes and the provenance that identifies them.

    Attributes:
        source_key: Configured key of the fetched source.
        source_type: `github` or `https`.
        source_locator: `github:<repository>:<path>`, or the configured URL even
            when redirects were followed.
        source_revision: Resolved commit SHA for GitHub sources, otherwise `None`.
        source_checksum: Lowercase SHA-256 of `content`.
        fetched_at: When retrieval completed.
        content: The downloaded bytes, before any decompression.
    """

    source_key: str
    source_type: Literal["github", "https"]
    source_locator: str
    source_revision: str | None
    source_checksum: str
    fetched_at: datetime
    content: bytes = field(repr=False)

    @property
    def provenance(self) -> SourceProvenance:
        """Return the provenance fields stored with every snapshot."""
        return SourceProvenance(
            source_type=self.source_type,
            source_locator=self.source_locator,
            source_revision=self.source_revision,
            source_checksum=self.source_checksum,
            fetched_at=self.fetched_at,
        )


class TerminalRefreshError(Exception):
    """Report a refresh failure that retrying cannot fix.

    `RefreshRunner` fails the job with `failure_code` and stores
    `failure_details`. Subclasses whose code never varies set `failure_code` as
    a class attribute and pass only their own message. Services translating a
    parser error raise this class with keyword arguments.

    Attributes:
        failure_code: Code recorded on the job and its failure audit event.
        failure_details: Optional JSON-compatible description, such as invalid
            rows. It must not contain URLs.
    """

    failure_code: RefreshFailureCode
    failure_details: dict[str, object] | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        failure_code: RefreshFailureCode | None = None,
        failure_details: dict[str, object] | None = None,
    ) -> None:
        if failure_code is not None:
            self.failure_code = failure_code
        elif not hasattr(self, "failure_code"):
            raise TypeError("a terminal refresh error needs a failure code")
        if failure_details is not None:
            self.failure_details = failure_details
        super().__init__(message or f"Refresh failed: {self.failure_code.value}")


class UnknownSourceError(InvalidInputError, TerminalRefreshError):
    """Report a source key that is not configured for a refresh kind.

    A refresh job fails with `failure_code`, and a request to start a refresh is
    rejected with one validation issue at `source_key`. Neither message repeats
    the key, so both can be logged safely.

    Attributes:
        kind: Refresh kind the key was looked up for.
        source_key: The unconfigured key.
    """

    failure_code = RefreshFailureCode.UNKNOWN_SOURCE
    code = "unknown_source"
    message = "Source is not configured"

    def __init__(self, kind: RefreshKindName, source_key: str) -> None:
        self.kind = kind
        self.source_key = source_key
        # Only `TerminalRefreshError` initializes the exception. It comes after
        # the SAB category in the bases, so its `super()` call reaches
        # `Exception` and the text the refresh runner sees is unchanged.
        TerminalRefreshError.__init__(self, "source is not configured")

    def issue_location(self) -> tuple[str | int, ...]:
        """Return the request body field that names the source."""
        return ("body", "source_key")


class RetryableRefreshError(Exception):
    """Report expected, temporary contention, such as a busy refresh lock.

    `RefreshRunner` leaves the job running and re-raises, so Celery retries the
    task later.
    """


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """Describe the committed result of one refresh or retirement.

    `RefreshRunner` adds `phase` to the progress and, for refresh jobs,
    `unchanged` to both progress and result.

    Attributes:
        result: Kind-specific JSON-compatible result. Never contains
            `unchanged`.
        counts: Counts stored as job progress.
        warnings: Short messages stored on the job.
        unchanged: Whether the document was already active, so nothing was
            applied.
    """

    result: dict[str, object]
    counts: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    unchanged: bool = False


class ProgressReporter(Protocol):
    """Record a running job's phase, counts, and warnings while a refresh applies.

    `RefreshRunner` passes one to each refresh service's `apply` and turns the
    reports into the job's progress, so services never build progress
    dictionaries.
    """

    def __call__(
        self,
        phase: str,
        counts: Mapping[str, int] | None = None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        """Store `phase`, `counts`, and `warnings` on the job."""
        ...


REFRESH_JOB_TYPES: Mapping[RefreshKindName, JobType] = {
    RefreshKindName.AUTHORIZATION: JobType.AUTHORIZATION_REFRESH,
    RefreshKindName.ONTOLOGY: JobType.ONTOLOGY_REFRESH,
    RefreshKindName.ENTITY: JobType.ENTITY_REFRESH,
    RefreshKindName.ANNOTATION: JobType.ANNOTATION_REFRESH,
}
"""Job type that refreshes one source of each kind.

Annotation cutovers use `JobType.ANNOTATION_CUTOVER` and are started only by
`RefreshStartService.start_cutover`.
"""

_SOURCE_KEY = re.compile(SOURCE_KEY_PATTERN)


def refresh_failure_message(kind: RefreshKindName | None) -> str:
    """Return the public error stored on a failed refresh job.

    Args:
        kind: The job's kind, or `None` when the job type is not a refresh.
    """
    if kind is None:
        return "Refresh failed"
    return f"{kind.value.capitalize()} refresh failed"


def refresh_dispatch_failure_message(kind: RefreshKindName) -> str:
    """Return the public error stored when a new refresh job cannot be sent."""
    return f"{kind.value.capitalize()} refresh could not be dispatched"


def job_source_key(parameters: Mapping[str, object]) -> str:
    """Return the `source_key` stored on a refresh or retirement job.

    Raises:
        ValueError: If the parameters are anything other than exactly one valid
            `source_key`.
    """
    source_key = parameters.get("source_key")
    if (
        set(parameters) != {"source_key"}
        or not isinstance(source_key, str)
        or _SOURCE_KEY.fullmatch(source_key) is None
    ):
        raise ValueError("invalid persisted refresh job parameters")
    return source_key
