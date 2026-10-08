"""Test the generated OpenAPI contract for annotation routes."""

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from standard_annotation_backend.domain.annotations import Annotation


def test_creation_ownership_is_optional(client: TestClient) -> None:
    """Clients may create an annotation without supplying `owning_group_id`."""
    schema = client.get("/openapi.json").json()["components"]["schemas"][
        "AnnotationCreateRequest"
    ]
    assert "owning_group_id" not in schema["required"]


def _openapi_statuses(*status_codes: int) -> set[str]:
    return {str(status_code) for status_code in status_codes}


def test_annotation_openapi_documents_routes_models_and_filters(
    client: TestClient,
) -> None:
    """OpenAPI documents every annotation route, response model, and query filter."""
    response = client.get("/openapi.json")

    assert response.status_code == status.HTTP_200_OK
    paths = response.json()["paths"]
    assert set(paths["/annotations"]) == {"get", "post"}
    assert set(paths["/annotations/{annotation_id}"]) == {
        "get",
        "patch",
        "delete",
    }
    assert set(paths["/annotations/{annotation_id}/versions"]) == {"get"}
    assert set(paths["/annotations/{annotation_id}/versions/{version}"]) == {"get"}

    ok = str(status.HTTP_200_OK)
    created = str(status.HTTP_201_CREATED)
    no_content = str(status.HTTP_204_NO_CONTENT)
    assert paths["/annotations"]["get"]["responses"][ok]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/AnnotationPageResponse")
    assert paths["/annotations"]["post"]["responses"][created]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/AnnotationResource")
    assert paths["/annotations/{annotation_id}"]["get"]["responses"][ok]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/AnnotationResource")
    assert paths["/annotations/{annotation_id}"]["patch"]["responses"][ok]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/AnnotationResource")
    assert (
        "content"
        not in paths["/annotations/{annotation_id}"]["delete"]["responses"][no_content]
    )
    assert paths["/annotations/{annotation_id}/versions"]["get"]["responses"][ok][
        "content"
    ]["application/json"]["schema"]["$ref"].endswith("/AnnotationVersionPageResponse")
    assert paths["/annotations/{annotation_id}/versions/{version}"]["get"]["responses"][
        ok
    ]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/AnnotationVersionResource"
    )

    collection_parameters = paths["/annotations"]["get"]["parameters"]
    assert {parameter["name"] for parameter in collection_parameters} == {
        "db_object_id",
        "negation",
        "relation",
        "ontology_class_id",
        "ontology_class_id_closure",
        "references",
        "evidence_type",
        "with_or_from",
        "interacting_taxon_id",
        "annotation_date",
        "assigned_by",
        "limit",
        "offset",
    }
    assert all(parameter["in"] == "query" for parameter in collection_parameters)


def test_annotation_patch_request_accepts_every_annotation_field(
    client: TestClient,
) -> None:
    """Clients may patch any annotation field, and no field is required."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    patch_schema = schemas["AnnotationPatchRequest"]

    assert set(patch_schema["properties"]) == set(
        Annotation.model_json_schema()["properties"]
    )
    assert "required" not in patch_schema


@pytest.mark.parametrize(
    ("path", "method", "expected_statuses"),
    [
        (
            "/annotations",
            "get",
            _openapi_statuses(
                status.HTTP_200_OK,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            ),
        ),
        (
            "/annotations",
            "post",
            _openapi_statuses(
                status.HTTP_201_CREATED,
                status.HTTP_409_CONFLICT,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            ),
        ),
        (
            "/annotations/{annotation_id}",
            "get",
            _openapi_statuses(
                status.HTTP_200_OK,
                status.HTTP_404_NOT_FOUND,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            ),
        ),
        (
            "/annotations/{annotation_id}",
            "patch",
            _openapi_statuses(
                status.HTTP_200_OK,
                status.HTTP_404_NOT_FOUND,
                status.HTTP_409_CONFLICT,
                status.HTTP_412_PRECONDITION_FAILED,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                status.HTTP_428_PRECONDITION_REQUIRED,
            ),
        ),
        (
            "/annotations/{annotation_id}",
            "delete",
            _openapi_statuses(
                status.HTTP_204_NO_CONTENT,
                status.HTTP_404_NOT_FOUND,
                status.HTTP_412_PRECONDITION_FAILED,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                status.HTTP_428_PRECONDITION_REQUIRED,
            ),
        ),
        (
            "/annotations/{annotation_id}/versions",
            "get",
            _openapi_statuses(
                status.HTTP_200_OK,
                status.HTTP_404_NOT_FOUND,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            ),
        ),
        (
            "/annotations/{annotation_id}/versions/{version}",
            "get",
            _openapi_statuses(
                status.HTTP_200_OK,
                status.HTTP_404_NOT_FOUND,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            ),
        ),
    ],
)
def test_annotation_openapi_documents_applicable_typed_errors(
    client: TestClient,
    path: str,
    method: str,
    expected_statuses: set[str],
) -> None:
    """OpenAPI lists only applicable statuses using the standard error model."""
    schema = client.get("/openapi.json").json()
    responses = schema["paths"][path][method]["responses"]
    assert set(responses) == expected_statuses | {"401", "403", "500", "503"}
    assert schema["paths"][path][method]["security"] == [{"Bearer": []}]
    assert (
        responses["401"]["headers"]["WWW-Authenticate"]["schema"]["const"] == "Bearer"
    )
    assert "`authentication_required`" in responses["401"]["description"]
    assert "`permission_denied`" in responses["403"]["description"]


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_annotation_openapi_requires_one_string_if_match(
    client: TestClient, method: str
) -> None:
    """OpenAPI requires one string-valued If-Match header for each write."""
    schema = client.get("/openapi.json").json()
    parameters = schema["paths"]["/annotations/{annotation_id}"][method]["parameters"]
    headers = [parameter for parameter in parameters if parameter["in"] == "header"]
    assert len(headers) == 1
    assert headers[0]["name"] == "If-Match"
    assert headers[0]["required"] is True
    assert headers[0]["schema"]["type"] == "string"
    assert headers[0]["schema"]["pattern"] == r'^"([1-9][0-9]*)"$'


@pytest.mark.parametrize(
    ("path", "method", "status_code", "expected_headers"),
    [
        (
            "/annotations",
            "post",
            str(status.HTTP_201_CREATED),
            {"ETag", "Location"},
        ),
        (
            "/annotations/{annotation_id}",
            "get",
            str(status.HTTP_200_OK),
            {"ETag"},
        ),
        (
            "/annotations/{annotation_id}",
            "patch",
            str(status.HTTP_200_OK),
            {"ETag"},
        ),
    ],
)
def test_annotation_openapi_documents_success_response_headers(
    client: TestClient,
    path: str,
    method: str,
    status_code: str,
    expected_headers: set[str],
) -> None:
    """OpenAPI documents version and location headers on successful responses."""
    schema = client.get("/openapi.json").json()
    response = schema["paths"][path][method]["responses"][status_code]
    assert set(response.get("headers", {})) == expected_headers
    for header in response["headers"].values():
        assert header["schema"]["type"] == "string"


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/annotations", "post"),
        ("/annotations/{annotation_id}", "patch"),
        ("/change-sets/{change_set_id}/accept", "post"),
    ],
)
def test_write_422_docs_list_validation_and_unknown_subject_codes(
    client: TestClient, path: str, method: str
) -> None:
    """Write routes document `request_validation_error` and `unknown_db_object_id` for 422."""
    responses = client.get("/openapi.json").json()["paths"][path][method]["responses"]

    description = responses["422"]["description"]
    assert "`request_validation_error`" in description
    assert "`unknown_db_object_id`" in description


def test_annotation_search_docs_list_query_errors_and_both_unavailable_causes(
    client: TestClient,
) -> None:
    """Search documents its located query errors and every 503 cause."""
    responses = client.get("/openapi.json").json()["paths"]["/annotations"]["get"][
        "responses"
    ]

    for code in (
        "unknown_query_parameter",
        "unsupported_filter",
        "closure_term_required",
        "unsupported_closure_field",
        "unsupported_closure_predicate",
        "request_validation_error",
    ):
        assert f"`{code}`" in responses["422"]["description"]
    for code in ("credential_storage_unavailable", "ontology_unavailable"):
        assert f"`{code}`" in responses["503"]["description"]


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_annotation_write_docs_list_if_match_errors(
    client: TestClient, method: str
) -> None:
    """Versioned writes document missing and malformed `If-Match` errors."""
    responses = client.get("/openapi.json").json()["paths"][
        "/annotations/{annotation_id}"
    ][method]["responses"]

    assert "`precondition_required`" in responses["428"]["description"]
    assert "`malformed_precondition`" in responses["422"]["description"]
    assert "`request_validation_error`" in responses["422"]["description"]
