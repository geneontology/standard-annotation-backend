"""Verify the public entity import creation contract."""

from fastapi import status
from fastapi.testclient import TestClient


def test_entity_import_openapi_documents_route_and_authorization(
    client: TestClient,
) -> None:
    """The operation is tagged, bearer-protected, and explicit about global scope."""
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/entity-imports"]["post"]

    tags = {tag["name"]: tag.get("description") for tag in schema["tags"]}
    assert tags["entity imports"] == "Create durable entity catalog imports."
    assert operation["tags"] == ["entity imports"]
    assert operation["security"] == [{"Bearer": []}]
    assert operation["summary"] == "Create entity imports"
    assert operation["description"] == (
        "Import one configured entity source, or omit source_key to import every "
        "configured source and retire catalogs whose source was removed. Existing "
        "queued or running jobs for the same source are returned instead of "
        "duplicated. Requires the admin role with global scope."
    )


def test_entity_import_openapi_documents_strict_request_constraints(
    client: TestClient,
) -> None:
    """The request schema allows only an optional `source_key` and rejects extra fields.

    The key must match the pattern used for registry keys.
    """
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/entity-imports"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/EntityImportRequest")

    request = schema["components"]["schemas"]["EntityImportRequest"]
    assert request["additionalProperties"] is False
    assert "required" not in request
    assert set(request["properties"]) == {"source_key"}
    source_key = request["properties"]["source_key"]
    assert {"type": "null"} in source_key["anyOf"]
    assert any(
        option.get("pattern") == "^[a-z0-9][a-z0-9_-]*$"
        for option in source_key["anyOf"]
    )
    assert "import every configured source" in source_key["description"]
    assert source_key["examples"] == ["mouse"]


def test_entity_import_openapi_documents_job_and_stable_responses(
    client: TestClient,
) -> None:
    """Success and expected failures have stable descriptions and typed bodies."""
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/entity-imports"]["post"]["responses"]

    assert set(responses) == {"202", "401", "403", "422", "503"}
    assert responses[str(status.HTTP_202_ACCEPTED)]["description"] == (
        "The created or reused entity jobs"
    )
    assert responses[str(status.HTTP_202_ACCEPTED)]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/EntityImportJobsResource")
    jobs = schema["components"]["schemas"]["EntityImportJobsResource"]["properties"][
        "jobs"
    ]
    assert jobs["type"] == "array"
    assert jobs["items"]["$ref"].endswith("/JobResource")
    expected_descriptions = {
        "401": "Authentication required",
        "403": "Permission denied",
        "422": (
            "Request validation failed, or source_key is not configured "
            "(unknown_entity_source)."
        ),
        "503": "Credential storage is unavailable",
    }
    for code, description in expected_descriptions.items():
        assert responses[code]["description"] == description
        assert responses[code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ApiErrorResponse"
        }

    assert "entity_import" in schema["components"]["schemas"]["JobType"]["enum"]


def test_entity_import_job_openapi_explains_terminal_diagnostics_and_warning_counts(
    client: TestClient,
) -> None:
    """The job schema documents failure codes, failure details, and warning counts."""
    properties = client.get("/openapi.json").json()["components"]["schemas"][
        "JobResource"
    ]["properties"]
    [example] = properties["progress"]["examples"]
    assert example["failure_code"] == "row_validation"
    assert example["failure_details"]["issues"][0]["line_number"] == 412
    description = properties["progress"]["description"]
    assert "failure_code" in description
    assert "failure_details" in description
    assert properties["result"]["examples"] == [{"warning_count": 0, "warnings": []}]
    assert "warning_count" in properties["result"]["description"]
    assert "unchanged: true" in properties["result"]["description"]
