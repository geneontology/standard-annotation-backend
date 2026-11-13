"""Verify the QC report issue catalog, findings, and stored results.

Each GO Rule's tests in `tests/unit/gorules` also verify that the catalog lists the
rule's issue type.
"""

from uuid import UUID

import pytest

from standard_annotation_backend.domain.qc_reports import (
    STORED_QC_REPORT_RESULT,
    EntityCatalogUse,
    IssueType,
    OntologySnapshotUse,
    QcFinding,
    QcReportResult,
    ReferenceDataVersions,
    issue_type_link,
    issue_type_of,
    issue_type_title,
)


@pytest.mark.parametrize("issue_type", list(IssueType))
def test_every_issue_type_has_a_title(issue_type: IssueType) -> None:
    """Each issue type has a stable, non-blank title."""
    assert issue_type_title(issue_type).strip()


def test_go_rules_link_to_their_rule_and_other_types_do_not() -> None:
    """A GO Rule links to its go-site page; catalog issue types have no link."""
    assert issue_type_link(IssueType.GORULE_0000002) == (
        "https://github.com/geneontology/go-site/blob/master/metadata/rules/"
        "gorule-0000002.md"
    )
    assert issue_type_link(IssueType.UNKNOWN_TERM) is None


def test_structural_issues_share_one_issue_type() -> None:
    """A Pydantic issue type is reported as `structural`; semantic types keep theirs."""
    assert (
        issue_type_of({"location": ("annotation",), "message": "x", "type": "missing"})
        is IssueType.STRUCTURAL
    )
    assert (
        issue_type_of(
            {"location": ("annotation",), "message": "x", "type": "unknown_entity"}
        )
        is IssueType.UNKNOWN_ENTITY
    )


def test_a_finding_lists_each_issue_type_once() -> None:
    """Repeated issue types on one annotation appear once, in catalog order."""
    finding = QcFinding(
        annotation_id=UUID(int=1),
        annotation_version=2,
        owning_group_id="MGI",
        created_by="curator",
        issues=(
            {
                "location": ("annotation", "references", 0),
                "message": "x",
                "type": "gorule_0000055",
            },
            {
                "location": ("annotation", "db_object_id"),
                "message": "x",
                "type": "unknown_entity",
            },
            {
                "location": ("annotation", "references", 1),
                "message": "x",
                "type": "gorule_0000055",
            },
        ),
    )

    assert finding.issue_types == (IssueType.UNKNOWN_ENTITY, IssueType.GORULE_0000055)


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
