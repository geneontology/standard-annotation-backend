"""Verify the public OpenAPI contract of the admin operations routes."""

import pytest

from standard_annotation_backend.main import app

REFRESH_PATHS = (
    "/admin/authorization-refreshes",
    "/admin/ontology-refreshes",
    "/admin/entity-refreshes",
    "/admin/annotation-refreshes",
    "/admin/annotation-cutovers",
)
SOURCE_KEY_PATHS = (
    "/admin/ontology-refreshes",
    "/admin/entity-refreshes",
    "/admin/annotation-refreshes",
)


@pytest.fixture(scope="module")
def schema() -> dict:
    return app.openapi()


@pytest.mark.parametrize("path", REFRESH_PATHS)
def test_refresh_operations_share_documented_response(schema: dict, path: str) -> None:
    """Each refresh route documents the same 202 response."""
    operation = schema["paths"][path]["post"]

    assert operation["tags"] == ["admin operations"]
    accepted = operation["responses"]["202"]
    assert accepted["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RefreshJobsResource"
    }


@pytest.mark.parametrize("path", SOURCE_KEY_PATHS)
def test_source_key_operations_document_the_shared_body_and_unknown_source(
    schema: dict, path: str
) -> None:
    """Ontology and entity refreshes accept an optional source key."""
    operation = schema["paths"][path]["post"]

    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RefreshRequest"
    }
    assert "unknown_source" in operation["responses"]["422"]["description"]


def test_authorization_refresh_takes_no_body(schema: dict) -> None:
    """The single authorization source needs no request body or source key."""
    operation = schema["paths"]["/admin/authorization-refreshes"]["post"]

    assert "requestBody" not in operation
    assert "422" not in operation["responses"]


def test_refresh_request_forbids_extra_fields_and_constrains_keys(schema: dict) -> None:
    """Clients learn that only an optional, pattern-checked key is accepted."""
    request = schema["components"]["schemas"]["RefreshRequest"]

    assert request["additionalProperties"] is False
    assert request.get("required", []) == []
    key = request["properties"]["source_key"]
    assert "^[a-z0-9][a-z0-9_-]*$" in str(key)


def test_job_resource_documents_exact_fields_and_vocabulary(schema: dict) -> None:
    """The job schema lists every public field and the allowed lifecycle values."""
    resource = schema["components"]["schemas"]["JobResource"]

    assert set(resource["properties"]) == {
        "job_id",
        "job_type",
        "status",
        "requested_by",
        "progress",
        "warnings",
        "result",
        "error",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
    }
    assert set(resource["required"]) == set(resource["properties"])
    assert schema["components"]["schemas"]["JobType"]["enum"] == [
        "annotation_cutover",
        "annotation_refresh",
        "authorization_refresh",
        "entity_refresh",
        "entity_retirement",
        "ontology_refresh",
    ]
    assert schema["components"]["schemas"]["JobStatus"]["enum"] == [
        "queued",
        "running",
        "succeeded",
        "failed",
    ]


def test_job_read_documents_path_parameter_and_typed_errors(schema: dict) -> None:
    """The job route documents a UUID path parameter and typed error bodies."""
    operation = schema["paths"]["/admin/jobs/{job_id}"]["get"]

    assert operation["tags"] == ["admin operations"]
    assert "200" in operation["responses"]
    assert operation["security"] == [{"Bearer": []}]
    [parameter] = operation["parameters"]
    assert parameter["name"] == "job_id"
    assert parameter["schema"]["format"] == "uuid"
    for code in ("401", "403", "404", "500", "503"):
        assert operation["responses"][code]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/ApiErrorResponse"}
    responses = operation["responses"]
    assert "`authentication_required`" in responses["401"]["description"]
    assert "`permission_denied`" in responses["403"]["description"]
    assert "`internal_error`" in responses["500"]["description"]
    assert "`credential_storage_unavailable`" in responses["503"]["description"]


@pytest.mark.parametrize("path", REFRESH_PATHS)
def test_refresh_operations_are_bearer_protected_with_typed_errors(
    schema: dict, path: str
) -> None:
    """Every documented failure uses the standard API error response."""
    operation = schema["paths"][path]["post"]
    expected = {"202", "401", "403", "500", "503"}
    if path in SOURCE_KEY_PATHS:
        expected.add("422")
    if path.startswith("/admin/annotation-"):
        expected |= {"409", "422"}

    assert operation["security"] == [{"Bearer": []}]
    assert set(operation["responses"]) == expected
    for code in sorted(expected - {"202"}):
        assert operation["responses"][code]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/ApiErrorResponse"}


def test_cutover_requires_a_source_key_and_documents_conflict(schema: dict) -> None:
    """The cutover route takes a required source key and documents 409."""
    operation = schema["paths"]["/admin/annotation-cutovers"]["post"]
    body = schema["components"]["schemas"]["AnnotationCutoverRequest"]

    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AnnotationCutoverRequest"
    }
    assert body["required"] == ["source_key"]
    assert body["additionalProperties"] is False
    assert "group_sab_managed" in operation["responses"]["409"]["description"]
    refresh = schema["paths"]["/admin/annotation-refreshes"]["post"]
    assert "group_sab_managed" in refresh["responses"]["409"]["description"]
