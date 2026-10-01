"""Verify the public job status OpenAPI contract."""

from fastapi import status
from fastapi.testclient import TestClient


def test_job_openapi_documents_one_authenticated_status_route(
    client: TestClient,
) -> None:
    """OpenAPI exposes job status reads without a public creation operation."""
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    assert "/jobs" not in paths
    assert set(paths["/jobs/{job_id}"]) == {"get"}

    operation = paths["/jobs/{job_id}"]["get"]
    assert operation["security"] == [{"Bearer": []}]
    assert operation["tags"] == ["jobs"]
    assert operation["parameters"] == [
        {
            "name": "job_id",
            "in": "path",
            "required": True,
            "schema": {
                "type": "string",
                "format": "uuid",
                "title": "Job Id",
            },
        }
    ]


def test_job_openapi_documents_exact_resource_and_vocabulary(
    client: TestClient,
) -> None:
    """The response schema lists every public field and allowed lifecycle value."""
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/jobs/{job_id}"]["get"]
    response_schema = operation["responses"][str(status.HTTP_200_OK)]["content"][
        "application/json"
    ]["schema"]
    assert response_schema["$ref"].endswith("/JobResource")

    resource = schema["components"]["schemas"]["JobResource"]
    assert set(resource["properties"]) == {
        "job_id",
        "job_type",
        "status",
        "requested_by",
        "progress",
        "warnings",
        "result",
        "artifact_uri",
        "error",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
    }
    assert set(resource["required"]) == set(resource["properties"])
    assert "parameters" not in resource["properties"]
    assert schema["components"]["schemas"]["JobType"]["enum"] == [
        "authorization_sync",
        "entity_catalog_retirement",
        "entity_import",
        "ontology_load",
    ]
    assert schema["components"]["schemas"]["JobStatus"]["enum"] == [
        "queued",
        "running",
        "succeeded",
        "failed",
    ]


def test_job_openapi_documents_typed_expected_errors(client: TestClient) -> None:
    """Expected failures use the public API error schema."""
    operation = client.get("/openapi.json").json()["paths"]["/jobs/{job_id}"]["get"]
    for status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ):
        error_schema = operation["responses"][str(status_code)]["content"][
            "application/json"
        ]["schema"]
        assert error_schema["$ref"].endswith("/ApiErrorResponse")
