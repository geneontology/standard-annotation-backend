"""Verify the QC report issue catalog, issue classification, and stored results."""

from uuid import UUID

import pytest

from standard_annotation_backend.domain.qc_reports import (
    ISSUE_TYPES,
    STORED_QC_REPORT_RESULT,
    EntityCatalogUse,
    OntologySnapshotUse,
    QcReportResult,
    ReferenceDataVersions,
    issue_types_of,
)
from standard_annotation_backend.domain.semantic_validation.rules import RULES
from standard_annotation_backend.domain.validation import (
    ValidationIssue,
    ValidationStage,
)


def _issue(issue_type: str) -> ValidationIssue:
    return ValidationIssue(location=("annotation",), message="x", type=issue_type)


def test_the_catalog_lists_structural_then_every_rule_in_order() -> None:
    """Reports count structural issues and each rule's type, in rule order."""
    assert [entry.issue_type for entry in ISSUE_TYPES] == [
        "structural",
        *(rule.issue_type for rule in RULES),
    ]
    assert all(entry.title.strip() for entry in ISSUE_TYPES)


def test_structural_issues_count_once_as_structural() -> None:
    """Every issue of a structural failure counts as `structural`, once."""
    issues = [_issue("missing"), _issue("string_type"), _issue("missing")]

    assert issue_types_of(ValidationStage.STRUCTURAL, issues) == ("structural",)


def test_semantic_types_count_once_each_in_catalog_order() -> None:
    """Repeated semantic types appear once, in catalog order."""
    issues = [
        _issue("gorule_0000055"),
        _issue("unknown_entity"),
        _issue("gorule_0000055"),
    ]

    assert issue_types_of(ValidationStage.SEMANTIC, issues) == (
        "unknown_entity",
        "gorule_0000055",
    )


def test_an_unregistered_semantic_type_is_an_error() -> None:
    """A semantic issue type missing from the catalog is never counted silently."""
    with pytest.raises(ValueError, match="missing"):
        issue_types_of(ValidationStage.SEMANTIC, [_issue("missing")])


def test_the_stored_result_round_trips() -> None:
    """A report result loads back equal from its job-result form."""
    versions = ReferenceDataVersions(
        ontologies=(
            OntologySnapshotUse("go", UUID(int=1), "releases/2026-11-01", "abc"),
        ),
        go_site_revision="d" * 40,
        entity_catalogs=(EntityCatalogUse("mgi", UUID(int=2)),),
    )
    result = QcReportResult(
        group_key=None,
        annotations_checked=10,
        annotations_with_issues=3,
        reference_data=(versions,),
        sabe_version="0.1.0",
        schema_version="0.1.1",
    )

    assert STORED_QC_REPORT_RESULT.load(result.to_job_result()) == result
