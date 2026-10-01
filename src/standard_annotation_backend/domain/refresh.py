"""Define the vocabulary shared by every reference data refresh.

SAB keeps three kinds of reference data current from external sources:
authorization from go-site `users.yaml`, ontologies from OBO files, and entities
from GPI files. Refreshing one source means fetching it and applying it. These
values are shared by every kind and need neither HTTP nor a database.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

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


class RefreshFailureCode(StrEnum):
    """List the codes recorded when a refresh job fails in a way retrying cannot fix.

    The codes are fixed strings, so job records and logs never contain source
    URLs or content. Operators use them to tell retrieval, decoding, parsing,
    and publication failures apart.
    """

    INVALID_PARAMETERS = "invalid_parameters"
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


class UnknownSourceError(LookupError):
    """Report a source key that is not configured for a refresh kind.

    The message does not repeat the key, so it can be logged safely.
    """

    def __init__(self, kind: RefreshKindName, source_key: str) -> None:
        self.kind = kind
        self.source_key = source_key
        super().__init__("source is not configured")


REFRESH_JOB_TYPES: Mapping[RefreshKindName, JobType] = {
    RefreshKindName.AUTHORIZATION: JobType.AUTHORIZATION_REFRESH,
    RefreshKindName.ONTOLOGY: JobType.ONTOLOGY_REFRESH,
    RefreshKindName.ENTITY: JobType.ENTITY_REFRESH,
}
"""Job type that refreshes one source of each kind."""

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
