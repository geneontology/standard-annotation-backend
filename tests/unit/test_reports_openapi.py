"""Verify the OpenAPI contract of the QC report routes."""

import pytest

from standard_annotation_backend.domain.qc_reports import ISSUE_TYPES
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

    assert enum_schema["enum"] == [entry.issue_type for entry in ISSUE_TYPES]
    descriptions = enum_schema["x-enum-descriptions"]
    assert len(descriptions) == len(ISSUE_TYPES)
    assert (
        "gorule-0000002.md" in descriptions[enum_schema["enum"].index("gorule_0000002")]
    )
    assert descriptions[enum_schema["enum"].index("structural")] == (
        "Annotation fails the Standard Annotation schema"
    )


def test_reports_are_listed_with_status_and_paging_parameters(schema: dict) -> None:
    """The list route documents its parameters, page schema, and errors."""
    operation = schema["paths"][START]["get"]

    assert operation["tags"] == ["reports"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/QcReportPageResponse"
    }
    assert set(operation["responses"]) == {"200", "401", "403", "422", "500", "503"}
    assert "`unknown_query_parameter`" in operation["responses"]["422"]["description"]
    assert {parameter["name"] for parameter in operation["parameters"]} == {
        "status",
        "limit",
        "offset",
    }
    item = schema["components"]["schemas"]["QcReportListItemResource"]
    assert "findings_available" in item["required"]


def test_issue_details_are_published_as_named_shapes(schema: dict) -> None:
    """Each rule's details shape is a named schema an issue's details may take."""
    details = schema["components"]["schemas"]["ApiValidationIssue"]["properties"][
        "details"
    ]
    referenced = {
        option["$ref"].rsplit("/", 1)[-1]
        for option in details["anyOf"]
        if "$ref" in option
    }

    assert referenced == {
        entry.details.__name__ for entry in ISSUE_TYPES if entry.details is not None
    }
    assert "type" in details["description"]


def test_issue_type_descriptions_name_their_details_shape(schema: dict) -> None:
    """A view can tell from the catalog which details shape each type sends."""
    parameters = schema["paths"][FINDINGS]["get"]["parameters"]
    issue_type = next(p for p in parameters if p["name"] == "issue_type")
    enum_schema = next(o for o in issue_type["schema"]["anyOf"] if "enum" in o)
    described = dict(
        zip(enum_schema["enum"], enum_schema["x-enum-descriptions"], strict=True)
    )

    assert described["obsolete_term"] == (
        "Term is obsolete (details: ObsoleteTermDetails)"
    )
    assert "PrimaryIdDetails" in described["gorule_0000030"]
    assert "details" not in described["unknown_term"]


def test_go_rule_titles_describe_what_sabe_checks(schema: dict) -> None:
    """A write-blocking GO Rule is not described as a warning."""
    parameters = schema["paths"][FINDINGS]["get"]["parameters"]
    issue_type = next(p for p in parameters if p["name"] == "issue_type")
    enum_schema = next(o for o in issue_type["schema"]["anyOf"] if "enum" in o)
    described = dict(
        zip(enum_schema["enum"], enum_schema["x-enum-descriptions"], strict=True)
    )

    assert described["gorule_0000065"].startswith(
        "No annotations to obsoletion candidate terms ("
    )
