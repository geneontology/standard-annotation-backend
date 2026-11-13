"""Define annotation QC reports: their issue types, findings, and results.

A QC (quality control) report validates every active annotation in its scope,
which is either all groups or one owning group. It records a finding for each
annotation with at least one issue; the finding lists that annotation's issues.
The report's counts by group and by issue type are kept permanently, but its
findings are deleted when a newer report with the same scope completes.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from pydantic import Field

from standard_annotation_backend.domain.errors import (
    ConflictError,
    GoneError,
    NotFoundError,
)
from standard_annotation_backend.domain.semantic_validation.rules import RULES
from standard_annotation_backend.domain.stored_json import StoredJson
from standard_annotation_backend.domain.validation import (
    ValidationIssue,
    ValidationStage,
)

type Count = Annotated[int, Field(ge=0)]

STRUCTURAL = "structural"
"""The issue type a report counts every structural issue under."""


@dataclass(frozen=True, slots=True)
class IssueTypeEntry:
    """Describe one issue type a QC report counts.

    Attributes:
        issue_type: The stable name, such as `obsolete_term` or `structural`.
        title: A stable, human-readable title.
        link: The go-site page of a GO Rule; `None` for other types.
        details: The `TypedDict` the issues' `details` follow, if any.
    """

    issue_type: str
    title: str
    link: str | None = None
    details: type | None = None


ISSUE_TYPES: tuple[IssueTypeEntry, ...] = (
    IssueTypeEntry(STRUCTURAL, "Annotation fails the Standard Annotation schema"),
    *(
        IssueTypeEntry(rule.issue_type, rule.title, rule.link, rule.details)
        for rule in RULES
    ),
)
"""Every issue type a report counts: `structural`, then each rule in `RULES` order.

Issues from semantic validation keep their rule's type. Every issue from structural
validation counts as `structural`, whatever its Pydantic type.
"""

_CATALOGED = frozenset(entry.issue_type for entry in ISSUE_TYPES)


def issue_types_of(
    stage: ValidationStage, issues: Iterable[ValidationIssue]
) -> tuple[str, ...]:
    """Return the issue types a report counts for one annotation's issues.

    Args:
        stage: The validation stage that produced the issues.
        issues: The annotation's issues.

    Returns:
        `("structural",)` for a structural failure. Otherwise each distinct
        issue type, in `ISSUE_TYPES` order.

    Raises:
        ValueError: If a semantic issue type is not in `ISSUE_TYPES`. Every rule
            is registered, so this is a programming error; the report fails
            instead of storing a wrong count.
    """
    if stage is ValidationStage.STRUCTURAL:
        return (STRUCTURAL,)
    found = {issue["type"] for issue in issues}
    unknown = sorted(found - _CATALOGED)
    if unknown:
        raise ValueError(f"Issue types not in the catalog: {', '.join(unknown)}")
    return tuple(entry.issue_type for entry in ISSUE_TYPES if entry.issue_type in found)


@dataclass(frozen=True, slots=True)
class QcFinding:
    """Describe one annotation with at least one issue.

    Attributes:
        annotation_id: The annotation.
        annotation_version: The version that was checked.
        owning_group_id: The annotation's owning group.
        created_by: Who created the annotation's first version. A reader whose
            authorization has `self` scope sees only findings for annotations
            they created.
        issues: Every issue, in the format write responses use.
        issue_types: Each issue type the report counts the finding under, once,
            in catalog order when recorded. A stored finding keeps the types it
            was recorded with.
    """

    annotation_id: UUID
    annotation_version: int
    owning_group_id: str
    created_by: str
    issues: tuple[ValidationIssue, ...]
    issue_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OntologySnapshotUse:
    """Identify one ontology snapshot a report read.

    Attributes:
        ontology_key: The configured ontology.
        version_id: The snapshot.
        document_version: The version the ontology file declares, if any.
        source_revision: The source's revision, if it has one.
    """

    ontology_key: str
    version_id: UUID
    document_version: str | None
    source_revision: str | None


@dataclass(frozen=True, slots=True)
class EntityCatalogUse:
    """Identify one active entity catalog snapshot a report read.

    Attributes:
        source_key: The configured entity source.
        snapshot_id: The catalog snapshot.
    """

    source_key: str
    snapshot_id: UUID


@dataclass(frozen=True, slots=True)
class ReferenceDataVersions:
    """Identify one set of reference data a report validated against.

    Attributes:
        ontologies: Each configured ontology's active snapshot.
        go_site_revision: The go-site commit of the active rule data, if known.
        entity_catalogs: Each active entity catalog snapshot.
    """

    ontologies: tuple[OntologySnapshotUse, ...]
    go_site_revision: str | None
    entity_catalogs: tuple[EntityCatalogUse, ...]


@dataclass(frozen=True, slots=True)
class QcReportResult:
    """Summarize a completed QC report, as stored on its job.

    Attributes:
        group_key: The group the report covered, or `None` for all groups.
        annotations_checked: Active annotations validated.
        annotations_with_issues: Annotations given a finding.
        reference_data: Every distinct set of reference data used, in the order
            first used, starting with the set that was active when the report
            started. The report looks up the active snapshots again for each
            batch of annotations, so a report that runs while reference data is
            refreshed lists more than one set.
        sabe_version: The SABE release that ran the report.
        schema_version: Version of the `go-standard-annotation-schema` package
            the report validated with.
    """

    group_key: str | None
    annotations_checked: Count
    annotations_with_issues: Count
    reference_data: tuple[ReferenceDataVersions, ...]
    sabe_version: str
    schema_version: str

    def to_job_result(self) -> dict[str, object]:
        """Return the result in job-storage format."""
        return STORED_QC_REPORT_RESULT.dump(self)


STORED_QC_REPORT_RESULT = StoredJson(QcReportResult, label="QC report result")
"""The stored form of a QC report's result."""


@dataclass(frozen=True, slots=True)
class QcGroupCounts:
    """Count one group's annotations in a report.

    Attributes:
        annotations_checked: The group's annotations that were validated.
        annotations_with_issues: The group's annotations given a finding.
    """

    annotations_checked: int
    annotations_with_issues: int


class QcReportNotFoundError(NotFoundError):
    """Report a report ID that names no QC report.

    Attributes:
        report_id: The report that was not found.
    """

    code = "report_not_found"
    message = "Report was not found"

    def __init__(self, report_id: UUID) -> None:
        self.report_id = report_id
        super().__init__()


class QcReportNotCompletedError(ConflictError):
    """Report that a report has no findings to read because it has not succeeded.

    Attributes:
        report_id: The report that has not succeeded.
    """

    code = "report_not_completed"
    message = "Report has not completed successfully"

    def __init__(self, report_id: UUID) -> None:
        self.report_id = report_id
        super().__init__()


class QcReportFindingsExpiredError(GoneError):
    """Report that a newer report of the same scope replaced this report's findings.

    The report's summary and counts remain available.

    Attributes:
        report_id: The report whose findings were deleted.
    """

    code = "report_findings_expired"
    message = "Report findings have expired"

    def __init__(self, report_id: UUID) -> None:
        self.report_id = report_id
        super().__init__()
