"""Define the vocabulary shared by every reference data refresh.

SABE keeps reference data current from external sources: authorization from
go-site `users.yaml`, rule data from six other go-site files, ontologies from OBO
files, entities from GPI files, and annotations from group GPAD files. Rule data
is what GO Rules, GO's official checks on annotation quality, compare annotations
against. Refreshing one source means fetching it and applying it. These values are
shared by every kind and need neither HTTP nor a database.
"""

from __future__ import annotations

import base64
import heapq
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from standard_annotation_backend.domain.errors import InvalidInputError
from standard_annotation_backend.domain.jobs import FollowUpJob, JobFailureCode, JobType
from standard_annotation_backend.domain.stored_json import StoredJson
from standard_annotation_backend.domain.validation import ValidationIssue

SOURCE_KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
"""Pattern for every configured source key, also enforced in SQL checks."""

AUTHORIZATION_SOURCE_KEY = "go-site"
"""Fixed key of the single authorization source."""

RULES_SOURCE_KEY = "go-site"
"""Fixed key of the single source of the go-site files that GO Rules read."""


class RefreshKindName(StrEnum):
    """Name each kind of reference data that SABE refreshes."""

    AUTHORIZATION = "authorization"
    ONTOLOGY = "ontology"
    ENTITY = "entity"
    ANNOTATION = "annotation"
    RULES = "rules"


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Identify the exact source document that a snapshot was built from.

    Attributes:
        source_type: `github` or `https`.
        source_locator: `github:<repository>:<path>`, `github:<repository>` for
            files packed with `pack_source_files`, or the configured URL.
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
        source_locator: `github:<repository>:<path>`, `github:<repository>` for
            files packed with `pack_source_files`, or the configured URL even when
            redirects were followed.
        source_revision: Resolved commit SHA for GitHub sources, otherwise `None`.
        source_checksum: Lowercase SHA-256 of `content`.
        fetched_at: When retrieval completed.
        content: The downloaded bytes, before any decompression. When a source
            reads several files from one commit, this is their combined content
            from `pack_source_files`.
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

    failure_code: JobFailureCode
    failure_details: dict[str, object] | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        failure_code: JobFailureCode | None = None,
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

    failure_code = JobFailureCode.UNKNOWN_SOURCE
    code = "unknown_source"
    message = "Source is not configured"

    def __init__(self, kind: RefreshKindName, source_key: str) -> None:
        self.kind = kind
        self.source_key = source_key
        # Only `TerminalRefreshError` initializes the exception. It comes after
        # the SABE category in the bases, so its `super()` call reaches
        # `Exception` and the text the refresh runner sees is unchanged.
        TerminalRefreshError.__init__(self, "source is not configured")

    def issue_location(self) -> tuple[str | int, ...]:
        """Return the request body field that names the source."""
        return ("body", "source_key")


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """Describe what one refresh's `apply` did, before its job is marked succeeded.

    `RefreshRunner` adds `phase` to the progress and `unchanged` to both progress
    and result, then commits them with the job's success in the transaction
    `apply` ran in.

    Attributes:
        result: Kind-specific JSON-compatible result. Never contains
            `unchanged`.
        counts: Counts stored as job progress.
        warnings: Short messages stored on the job.
        unchanged: Whether the document was already active, so nothing was
            applied.
        follow_ups: Jobs that `apply` queued, such as a term replacement job
            after an ontology activation. The runner sends them to workers after
            the transaction commits.
    """

    result: dict[str, object]
    counts: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    unchanged: bool = False
    follow_ups: tuple[FollowUpJob, ...] = ()


REFRESH_JOB_TYPES: Mapping[RefreshKindName, JobType] = {
    RefreshKindName.AUTHORIZATION: JobType.AUTHORIZATION_REFRESH,
    RefreshKindName.ONTOLOGY: JobType.ONTOLOGY_REFRESH,
    RefreshKindName.ENTITY: JobType.ENTITY_REFRESH,
    RefreshKindName.ANNOTATION: JobType.ANNOTATION_REFRESH,
    RefreshKindName.RULES: JobType.RULES_REFRESH,
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


MAX_REPORTED_ISSUES = 100
"""Maximum number of validation issues or rejected rows listed for one refresh job."""


def validation_failure_details(issues: Sequence[ValidationIssue]) -> dict[str, object]:
    """Return a document's validation issues as failure details for a refresh job.

    Every issue is counted, but only the first 100 are kept, so a document with
    many problems cannot make the job record very large. Each kept issue has its
    `location`, `message`, and `type`.
    """
    return {
        "issue_count": len(issues),
        "issues": [
            {
                "location": list(issue["location"]),
                "message": issue["message"],
                "type": issue["type"],
            }
            for issue in issues[:MAX_REPORTED_ISSUES]
        ],
    }


MAX_REJECTION_REASON_LENGTH = 1000
"""Maximum length, in characters, of the `reason` stored for one rejected row.

The message stored for an invalid GPI or GPAD header has the same limit. Both can
repeat values from the source file, so one oversized field could otherwise put
megabytes into a job record, its audit event, and every API response that returns
the job.
"""


@dataclass(frozen=True, slots=True)
class RowRejection:
    """Describe one data row of a GPI or GPAD file that a refresh did not load.

    Attributes:
        line_number: One-based line number in the source file.
        code: Why the row was rejected. `syntax`, `field-count`, and
            `validation` mean the row could not be read or failed schema
            validation. A GPI row can also be rejected as `duplicate`, when
            another row lists the same `db_object_id`, or `collision`, when
            another source has that `db_object_id` active.
        reason: Short explanation, at most 1,000 characters.
    """

    line_number: Annotated[int, Field(ge=0)]
    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class RejectionReport:
    """Summarize every row a GPI or GPAD refresh rejected.

    Attributes:
        issue_count: Number of rejected rows.
        by_code: Number of rejected rows for each code.
        issues: The rejected rows with the lowest line numbers, at most 100.
    """

    issue_count: Annotated[int, Field(ge=0)]
    by_code: Mapping[str, Annotated[int, Field(ge=0)]]
    issues: tuple[RowRejection, ...]


class RejectionReportBuilder:
    """Build a `RejectionReport` from rejections added one at a time.

    Every rejection is counted, but only the 100 with the lowest line numbers
    are kept, so a file with millions of bad rows uses little memory.

    Rejections may be added in any line order, and the report still lists the
    lowest lines. Rejections from the same line keep the order in which they were
    added.
    """

    def __init__(self) -> None:
        self._issue_count = 0
        self._by_code: Counter[str] = Counter()
        # The kept rejections, keyed by negated (line number, arrival). `heapq`
        # is a min-heap, so negating the keys puts the highest line kept (and,
        # within a line, the latest arrival) at `self._kept[0]`: the entry to
        # evict when a rejection with a lower line number arrives.
        self._kept: list[tuple[int, int, RowRejection]] = []

    def add(self, rejection: RowRejection) -> None:
        """Count one rejection and keep it if it is among the 100 lowest lines."""
        self._issue_count += 1
        self._by_code[rejection.code] += 1
        entry = (-rejection.line_number, -self._issue_count, rejection)
        if len(self._kept) < MAX_REPORTED_ISSUES:
            heapq.heappush(self._kept, entry)
        elif entry[:2] > self._kept[0][:2]:
            heapq.heapreplace(self._kept, entry)

    def build(self) -> RejectionReport:
        """Return the report of everything added so far."""
        kept = sorted(self._kept, key=lambda entry: (-entry[0], -entry[1]))
        return RejectionReport(
            issue_count=self._issue_count,
            by_code=dict(sorted(self._by_code.items())),
            issues=tuple(entry[2] for entry in kept),
        )


STORED_REJECTION_REPORT = StoredJson(RejectionReport, label="rejection report")


class NoValidRowsError(TerminalRefreshError):
    """Report a GPI or GPAD file with no row that can be loaded.

    Loading it would replace the source's data with nothing, which usually means
    a wrong or broken file, so the refresh fails and the previous data stays. A
    file with no data rows at all fails the same way. The job's failure details
    are the rejection report.

    Attributes:
        report: The rows that were rejected.
    """

    failure_code = JobFailureCode.NO_VALID_ROWS

    def __init__(self, report: RejectionReport) -> None:
        self.report = report
        super().__init__(
            "source file has no valid rows",
            failure_details=STORED_REJECTION_REPORT.dump(report),
        )


def job_source_key(parameters: Mapping[str, object]) -> str:
    """Return the `source_key` stored on a refresh job.

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


def pack_source_files(files: Mapping[str, bytes]) -> bytes:
    """Combine files fetched from one commit into the content of one document.

    A refresh kind that reads several files from the same commit, such as
    `rules`, receives them as one `SourceDocument`, so they share one provenance
    and one checksum. The content is JSON that maps each path to its
    base64-encoded bytes. Paths are sorted, so the same files always give the same
    content and checksum.

    Args:
        files: The downloaded bytes of each file, keyed by path.
    """
    return json.dumps(
        {path: base64.b64encode(files[path]).decode("ascii") for path in sorted(files)},
        separators=(",", ":"),
    ).encode("ascii")


def unpack_source_files(content: bytes) -> dict[str, bytes]:
    """Return the files that `pack_source_files` combined.

    Raises:
        ValueError: If `content` is not in the form `pack_source_files` writes.
    """
    try:
        packed = json.loads(content)
    except ValueError:
        raise ValueError("content is not packed source files") from None
    if not isinstance(packed, dict) or not all(
        isinstance(value, str) for value in packed.values()
    ):
        raise ValueError("content is not packed source files")
    try:
        return {
            path: base64.b64decode(value, validate=True)
            for path, value in packed.items()
        }
    except ValueError:
        raise ValueError("content is not packed source files") from None
