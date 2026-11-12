"""Verify the OpenAPI contract of the QC report routes."""

import pytest

from standard_annotation_backend.domain.qc_reports import IssueType
from standard_annotation_backend.main import app

START = "/reports/annotation-qc"
SUMMARY = "/reports/annotation-qc/{report_id}"
FINDINGS = "/reports/annotation-qc/{report_id}/findings"


@pytest.fixture(scope="module")
def schema() -> dict:
    return app.openapi()


def test_starting_a_report_returns_202_with_the_job(schema: dict) -> None:
    """The start route documents its job response, body, and errors."""
    operation = schema["paths"][START]["post"]

    assert operation["tags"] == ["reports"]
    assert operation["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/JobResource"
    }
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/QcReportRequest"
    }
    assert set(operation["responses"]) == {"202", "401", "403", "422", "500", "503"}


@pytest.mark.parametrize(
    ("path", "statuses"),
    [
        (SUMMARY, {"200", "401", "403", "404", "422", "500", "503"}),
        (FINDINGS, {"200", "401", "403", "404", "409", "410", "422", "500", "503"}),
    ],
)
def test_read_routes_document_their_errors(
    schema: dict, path: str, statuses: set[str]
) -> None:
    """The summary and findings routes list exactly the statuses they can return."""
    responses = schema["paths"][path]["get"]["responses"]

    assert set(responses) == statuses
    assert "`report_not_found`" in responses["404"]["description"]


def test_findings_document_expired_and_unfinished_reports(schema: dict) -> None:
    """Clients can tell expired findings from a report that has not finished."""
    responses = schema["paths"][FINDINGS]["get"]["responses"]

    assert "`report_findings_expired`" in responses["410"]["description"]
    assert "`report_not_completed`" in responses["409"]["description"]
    assert "`unknown_query_parameter`" in responses["422"]["description"]


def test_the_issue_type_filter_is_an_enum_with_titles_and_links(schema: dict) -> None:
    """Every issue type is listed with a description a view can show."""
    parameters = schema["paths"][FINDINGS]["get"]["parameters"]
    issue_type = next(
        parameter for parameter in parameters if parameter["name"] == "issue_type"
    )
    enum_schema = next(
        option for option in issue_type["schema"]["anyOf"] if "enum" in option
    )

    assert enum_schema["enum"] == [member.value for member in IssueType]
    descriptions = enum_schema["x-enum-descriptions"]
    assert len(descriptions) == len(IssueType)
    assert (
        "gorule-0000002.md" in descriptions[enum_schema["enum"].index("gorule_0000002")]
    )
    assert descriptions[enum_schema["enum"].index("structural")] == (
        "Annotation fails the Standard Annotation schema"
    )
