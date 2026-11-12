"""Describe annotation QC reports: their issue types, findings, and results.

A QC report validates every active annotation in its scope, all groups or one
owning group, and keeps one finding for each annotation with at least one issue.
Its counts by group and by issue type are kept permanently; its findings are
kept only until a newer report of the same scope completes.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field

from standard_annotation_backend.domain.errors import (
    ConflictError,
    GoneError,
    NotFoundError,
)
from standard_annotation_backend.domain.stored_json import StoredJson
from standard_annotation_backend.domain.validation import ValidationIssue

type Count = Annotated[int, Field(ge=0)]

_GO_RULE_URL = (
    "https://github.com/geneontology/go-site/blob/master/metadata/rules/{rule}.md"
)


class IssueType(StrEnum):
    """Name every kind of issue a QC report counts.

    Semantic issues keep their own type. Every structural issue, whatever its
    Pydantic type, counts as `structural`.
    """

    UNKNOWN_ENTITY = "unknown_entity"
    UNKNOWN_TERM = "unknown_term"
    OBSOLETE_TERM = "obsolete_term"
    ALTERNATE_TERM_ID = "alternate_term_id"
    DUPLICATE_ANNOTATION = "duplicate_annotation"
    STRUCTURAL = "structural"
    GORULE_0000002 = "gorule_0000002"
    GORULE_0000005 = "gorule_0000005"
    GORULE_0000006 = "gorule_0000006"
    GORULE_0000007 = "gorule_0000007"
    GORULE_0000008 = "gorule_0000008"
    GORULE_0000011 = "gorule_0000011"
    GORULE_0000015 = "gorule_0000015"
    GORULE_0000016 = "gorule_0000016"
    GORULE_0000017 = "gorule_0000017"
    GORULE_0000018 = "gorule_0000018"
    GORULE_0000022 = "gorule_0000022"
    GORULE_0000027 = "gorule_0000027"
    GORULE_0000030 = "gorule_0000030"
    GORULE_0000033 = "gorule_0000033"
    GORULE_0000037 = "gorule_0000037"
    GORULE_0000039 = "gorule_0000039"
    GORULE_0000042 = "gorule_0000042"
    GORULE_0000043 = "gorule_0000043"
    GORULE_0000046 = "gorule_0000046"
    GORULE_0000050 = "gorule_0000050"
    GORULE_0000053 = "gorule_0000053"
    GORULE_0000055 = "gorule_0000055"
    GORULE_0000058 = "gorule_0000058"
    GORULE_0000061 = "gorule_0000061"
    GORULE_0000063 = "gorule_0000063"
    GORULE_0000065 = "gorule_0000065"


# GO Rule titles follow the rule's go-site page.
_TITLES: Mapping[IssueType, str] = {
    IssueType.UNKNOWN_ENTITY: "Subject is not an active entity",
    IssueType.UNKNOWN_TERM: "Identifier is not a term of its ontology",
    IssueType.OBSOLETE_TERM: "Term is obsolete",
    IssueType.ALTERNATE_TERM_ID: "Identifier is an alternative ID",
    IssueType.DUPLICATE_ANNOTATION: "Annotation duplicates other annotations",
    IssueType.STRUCTURAL: "Annotation fails the Standard Annotation schema",
    IssueType.GORULE_0000002: (
        "No NOT annotations to GO:0005488 binding or GO:0005515 protein binding"
    ),
    IssueType.GORULE_0000005: (
        "IEA, ISS, ISO, ISM, ISA, IBA, RCA not allowed for direct binding or "
        "protein binding"
    ),
    IssueType.GORULE_0000006: "IEP and HEP only for Biological Process terms",
    IssueType.GORULE_0000007: (
        "IPI should not be used with GO:0003824 catalytic activity or descendants"
    ),
    IssueType.GORULE_0000008: "No annotations to uninformative high-level terms",
    IssueType.GORULE_0000011: "ND only on root terms, and root terms only with ND",
    IssueType.GORULE_0000015: "Dual species taxon check",
    IssueType.GORULE_0000016: "IC annotations require a With/From GO ID",
    IssueType.GORULE_0000017: "IDA annotations must not have a With/From entry",
    IssueType.GORULE_0000018: "IPI annotations require a With/From entry",
    IssueType.GORULE_0000022: "Filter annotations to retracted publications",
    IssueType.GORULE_0000027: "Each identifier in GAF is valid",
    IssueType.GORULE_0000030: "Only valid GO_REFs are allowed",
    IssueType.GORULE_0000033: "Only public reference IDs (PMID, DOI, GO_REF)",
    IssueType.GORULE_0000037: "IBA only assigned_by GO_Central with GO_REF:0000033",
    IssueType.GORULE_0000039: (
        "Protein complexes cannot be annotated to GO:0032991 or descendants"
    ),
    IssueType.GORULE_0000042: "IKR evidence requires a NOT qualifier",
    IssueType.GORULE_0000043: (
        "Check for valid combination of evidence code, with/from rules and GO_REF"
    ),
    IssueType.GORULE_0000046: (
        "With/from must equal the gene product for self-binding terms"
    ),
    IssueType.GORULE_0000050: "ISS, ISA, ISO should not be self-referential",
    IssueType.GORULE_0000053: "NOT annotations should not have extensions",
    IssueType.GORULE_0000055: "References should have only one ID per ID space",
    IssueType.GORULE_0000058: "Extensions conform to extensions-constraints.yaml",
    IssueType.GORULE_0000061: "Allowed gene product to term relations",
    IssueType.GORULE_0000063: "ISS/ISA/ISO evidence requires a with/from gene product",
    IssueType.GORULE_0000065: "Annotations to obsoletion candidates produce a warning",
}


def issue_type_title(issue_type: IssueType) -> str:
    """Return the stable, human-readable title of an issue type."""
    return _TITLES[issue_type]


def issue_type_link(issue_type: IssueType) -> str | None:
    """Return the go-site page of a GO Rule issue type, or `None` for other types."""
    if not issue_type.startswith("gorule_"):
        return None
    return _GO_RULE_URL.format(rule=issue_type.value.replace("_", "-"))


def issue_type_of(issue: ValidationIssue) -> IssueType:
    """Return the issue type a report counts an issue under.

    Semantic issue types are listed in `IssueType`; every other type comes from
    structural validation.
    """
    try:
        return IssueType(issue["type"])
    except ValueError:
        return IssueType.STRUCTURAL


@dataclass(frozen=True, slots=True)
class QcFinding:
    """Describe one annotation with at least one issue.

    Attributes:
        annotation_id: The annotation.
        annotation_version: The version that was checked.
        owning_group_id: The annotation's owning group.
        created_by: The actor of the annotation's first version, which decides
            whether a self-scoped reader may see the finding.
        issues: Every issue, in the format write responses use.
    """

    annotation_id: UUID
    annotation_version: int
    owning_group_id: str
    created_by: str
    issues: tuple[ValidationIssue, ...]

    @property
    def issue_types(self) -> tuple[IssueType, ...]:
        """Return each issue type the finding has, once, in catalog order."""
        found = {issue_type_of(issue) for issue in self.issues}
        return tuple(issue_type for issue_type in IssueType if issue_type in found)


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
    """Identify one active entity catalog snapshot a report read."""

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
            first used, starting with the set resolved when the report started.
            Snapshots are resolved per batch, so a report that spans a refresh
            lists more than one.
        sabe_version: The SABE release that ran the report.
        schema_version: The Standard Annotation schema package version.
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
