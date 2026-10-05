"""Define the values and errors used to import a group's annotations from GPAD.

Each annotation source in `config/sources.yaml` names the group that owns every
annotation in its GPAD file. While the group is `gpad_imported`, that file is the
source of truth: each successful refresh deletes all of the group's annotations
and imports the file. A cutover is a final import that succeeds only if every
record is imported. It moves the group to `sab_managed` permanently, after which
the group's annotations are maintained in SAB and GPAD can no longer replace them.

An import has two steps. Staging reads the file and stores its valid annotations
in separate staging tables without touching the group's annotations. Publication
then replaces the group's annotations with the staged ones in one transaction.

These values need neither HTTP nor a database.
"""

from __future__ import annotations

import heapq
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from standard_annotation_backend.domain.refresh import SourceProvenance

MAX_REPORTED_REJECTIONS = 100
"""Maximum number of rejected records listed in a report.

The report's counts still include every rejected record.
"""

MAX_REJECTION_REASON_LENGTH = 200
"""Maximum length, in characters, of the `reason` stored for one rejected record."""

UNKNOWN_DB_OBJECT_ID = "unknown_db_object_id"
"""Rejection code for an annotation whose subject is not an active entity."""


class AnnotationManagementMode(StrEnum):
    """Name the source of truth for a group's annotations.

    `gpad_imported` means the group's GPAD file; `sab_managed` means SAB. A group
    without a stored mode is `gpad_imported`. The change to `sab_managed` happens
    only when a cutover publishes, and is permanent.
    """

    GPAD_IMPORTED = "gpad_imported"
    SAB_MANAGED = "sab_managed"


@dataclass(frozen=True, slots=True)
class AnnotationRejection:
    """Describe one GPAD record that was not imported.

    Attributes:
        line_number: One-based line number in the source file.
        code: `syntax`, `field-count`, `validation`, or `unknown_db_object_id`.
        reason: Short explanation, at most 200 characters.
    """

    line_number: int
    code: str
    reason: str


def unknown_subject_rejection(
    line_number: int, db_object_id: str
) -> AnnotationRejection:
    """Return the rejection for an annotation whose subject is not an active entity.

    Args:
        line_number: GPAD line the annotation came from.
        db_object_id: The annotation's subject, named in the reason.
    """
    reason = f"db_object_id {db_object_id} is not an active entity"
    return AnnotationRejection(
        line_number, UNKNOWN_DB_OBJECT_ID, reason[:MAX_REJECTION_REASON_LENGTH]
    )


@dataclass(frozen=True, slots=True)
class RejectionReport:
    """Summarize every record a GPAD refresh rejected.

    Attributes:
        issue_count: Number of rejected records.
        by_code: Number of rejected records for each code.
        issues: The rejected records with the lowest line numbers, at most 100.
    """

    issue_count: int
    by_code: Mapping[str, int]
    issues: tuple[AnnotationRejection, ...]

    def to_json(self) -> dict[str, object]:
        """Return the JSON-compatible form stored on jobs and imports."""
        return {
            "issue_count": self.issue_count,
            "by_code": dict(sorted(self.by_code.items())),
            "issues": [
                {
                    "line_number": issue.line_number,
                    "code": issue.code,
                    "reason": issue.reason,
                }
                for issue in self.issues
            ],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> RejectionReport:
        """Rebuild a report from the form returned by `to_json`.

        Raises:
            ValueError: If the value does not have that form.
        """
        by_code = value.get("by_code")
        issues = value.get("issues")
        if not isinstance(by_code, dict) or not isinstance(issues, list):
            raise ValueError("invalid stored rejection report")
        counts: dict[str, int] = {}
        for code, count in by_code.items():
            if not isinstance(code, str):
                raise ValueError("invalid stored rejection report")
            counts[code] = _count(count)
        parsed: list[AnnotationRejection] = []
        for issue in issues:
            if not isinstance(issue, dict):
                raise ValueError("invalid stored rejection report")
            code, reason = issue.get("code"), issue.get("reason")
            if not isinstance(code, str) or not isinstance(reason, str):
                raise ValueError("invalid stored rejection report")
            parsed.append(
                AnnotationRejection(_count(issue.get("line_number")), code, reason)
            )
        return cls(
            issue_count=_count(value.get("issue_count")),
            by_code=counts,
            issues=tuple(parsed),
        )


class RejectionReportBuilder:
    """Build a `RejectionReport` from rejections added one at a time.

    Every rejection is counted, but only the 100 with the lowest line numbers
    are kept, so a file with millions of bad rows uses little memory.

    Rejections may be added in any line order, and the report still lists the
    lowest lines. Staging, for example, adds a batch's unreadable rows first and
    its unknown-entity rejections after checking the whole batch against the
    entity catalog, so a rejection on line 12 can arrive after one on line 4,900.
    Rejections from the same line keep the order in which they were added.
    """

    def __init__(self) -> None:
        self._issue_count = 0
        self._by_code: Counter[str] = Counter()
        # The kept rejections, keyed by negated (line number, arrival). `heapq`
        # is a min-heap, so negating the keys puts the highest line kept (and,
        # within a line, the latest arrival) at `self._kept[0]`: the entry to
        # evict when a rejection with a lower line number arrives.
        self._kept: list[tuple[int, int, AnnotationRejection]] = []

    def add(self, rejection: AnnotationRejection) -> None:
        """Count one rejection and keep it if it is among the 100 lowest lines."""
        self._issue_count += 1
        self._by_code[rejection.code] += 1
        entry = (-rejection.line_number, -self._issue_count, rejection)
        if len(self._kept) < MAX_REPORTED_REJECTIONS:
            heapq.heappush(self._kept, entry)
        elif entry[:2] > self._kept[0][:2]:
            heapq.heapreplace(self._kept, entry)

    def build(self) -> RejectionReport:
        """Return the report of everything added so far."""
        kept = sorted(self._kept, key=lambda entry: (-entry[0], -entry[1]))
        return RejectionReport(
            issue_count=self._issue_count,
            by_code=dict(self._by_code),
            issues=tuple(entry[2] for entry in kept),
        )


@dataclass(frozen=True, slots=True)
class AnnotationRefreshResult:
    """Describe a published GPAD import.

    Attributes:
        source_key: Configured annotation source.
        group_key: Group whose annotations were replaced.
        import_job_id: Job that published the import.
        is_cutover: Whether the import was a cutover, which moved the group to
            SAB management.
        provenance: The exact source document that was imported.
        data_rows: Number of GPAD data rows read, including rejected ones.
        annotations_published: Number of annotations the import published.
        records_rejected: Number of records that were not imported.
        annotations_deleted: Number of the group's earlier annotations that the
            import deleted.
        mode: The group's mode after publication.
        rejection_report: Report of the records that were not imported.
    """

    source_key: str
    group_key: str
    import_job_id: UUID
    is_cutover: bool
    provenance: SourceProvenance
    data_rows: int
    annotations_published: int
    records_rejected: int
    annotations_deleted: int
    mode: AnnotationManagementMode
    rejection_report: RejectionReport

    def to_job_result(self) -> dict[str, object]:
        """Return the JSON-compatible result stored on the job."""
        return {
            "source_key": self.source_key,
            "group_key": self.group_key,
            "import_job_id": str(self.import_job_id),
            "is_cutover": self.is_cutover,
            "source_type": self.provenance.source_type,
            "source_locator": self.provenance.source_locator,
            "source_revision": self.provenance.source_revision,
            "source_checksum": self.provenance.source_checksum,
            "fetched_at": self.provenance.fetched_at.isoformat(),
            "data_rows": self.data_rows,
            "annotations_published": self.annotations_published,
            "records_rejected": self.records_rejected,
            "annotations_deleted": self.annotations_deleted,
            "mode": self.mode.value,
            "rejection_report": self.rejection_report.to_json(),
        }

    def to_progress(self) -> dict[str, object]:
        """Return the counts reported as job progress."""
        return {
            "data_rows": self.data_rows,
            "annotations_published": self.annotations_published,
            "records_rejected": self.records_rejected,
            "annotations_deleted": self.annotations_deleted,
        }


@dataclass(frozen=True, slots=True)
class AnnotationRefreshUnchanged:
    """Describe a refresh that was skipped because nothing would change.

    The fetched file matches the group's last import, and the group has not
    changed since.

    Attributes:
        source_key: Configured annotation source.
        group_key: Group whose annotations were left as they are.
        import_job_id: Job that published the group's current annotations.
        source_checksum: Checksum of the unchanged document.
    """

    source_key: str
    group_key: str
    import_job_id: UUID
    source_checksum: str

    def to_job_result(self) -> dict[str, object]:
        """Return the JSON-compatible result stored on the job."""
        return {
            "source_key": self.source_key,
            "group_key": self.group_key,
            "import_job_id": str(self.import_job_id),
            "source_checksum": self.source_checksum,
        }


class GroupSabManagedError(RuntimeError):
    """Report that SAB, not GPAD, is the source of truth for a group.

    GPAD may never replace a `sab_managed` group's annotations. The message does
    not name the group, so it can be logged and returned to API clients as is.

    Attributes:
        group_key: The SAB-managed group.
    """

    def __init__(self, group_key: str) -> None:
        self.group_key = group_key
        super().__init__("group is SAB-managed")


class NoValidAnnotationsError(ValueError):
    """Report a GPAD file with no annotations that can be imported.

    Publishing it would delete the group's annotations and import nothing. That
    usually means a wrong file or a missing entity catalog, so the refresh fails.

    Attributes:
        report: The records that were rejected.
    """

    def __init__(self, report: RejectionReport) -> None:
        self.report = report
        super().__init__("GPAD file has no valid annotations")


class CutoverRejectedError(ValueError):
    """Report a cutover that rejected at least one record.

    A cutover publishes only when every record is imported. Staging raises this
    for any rejected record, and publication raises it if a staged annotation's
    subject is no longer an active entity.

    Attributes:
        report: The records that were rejected.
    """

    def __init__(self, report: RejectionReport) -> None:
        self.report = report
        super().__init__("cutover requires zero rejected records")


class AnnotationImportConflictError(RuntimeError):
    """Report a job that cannot be staged or published.

    The job does not exist, is not a GPAD job, or has no staged import.
    """

    def __init__(self) -> None:
        super().__init__("job has no usable annotation import")


def _count(value: object) -> int:
    """Return a stored count or line number, or raise `ValueError`.

    The value must be a nonnegative integer; `bool` is rejected.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("invalid stored rejection report")
    return value
