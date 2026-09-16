"""Test the generated OpenAPI contract for annotation routes."""

import pytest
from fastapi import status
from fastapi.testclient import TestClient


def _openapi_statuses(*status_codes: int) -> set[str]:
    return {str(status_code) for status_code in status_codes}


def test_annotation_writes_include_realistic_request_examples(
    client: TestClient,
) -> None:
    """Create and patch operations show complete workflow-specific requests."""
    paths = client.get("/openapi.json").json()["paths"]
    create_content = paths["/annotations"]["post"]["requestBody"]["content"][
        "application/json"
    ]
    patch_content = paths["/annotations/{annotation_id}"]["patch"]["requestBody"][
        "content"
    ]["application/json"]

    assert set(create_content["examples"]) == {"create-annotation"}
    assert create_content["examples"]["create-annotation"]["value"] == {
        "owning_group_id": "GO_Central",
        "annotation": {
            "db_object_id": "UniProtKB:P12345",
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0008150",
            "references": ["PMID:12345678"],
            "evidence_type": "ECO:0000314",
            "with_or_from": ["UniProtKB:Q9XYZ1"],
            "interacting_taxon_id": ["NCBITaxon:9606"],
            "annotation_date": "2026-09-15",
            "assigned_by": "GO_Central",
            "annotation_extensions": [
                {
                    "extension_relation": "BFO:0000050",
                    "extension_term": "CL:0000000",
                }
            ],
            "annotation_properties": {
                "comment": ["Curated from the cited publication"]
            },
        },
    }
    assert set(patch_content["examples"]) == {"replace-annotation-fields"}
    assert patch_content["examples"]["replace-annotation-fields"]["value"] == {
        "references": ["PMID:12345678", "GO_REF:0000002"],
        "annotation_date": "2026-09-15",
    }


def test_annotation_openapi_documents_routes_models_headers_and_filters(
    client: TestClient,
) -> None:
    """OpenAPI documents every annotation route, model, header, and filter."""
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
        "references",
        "evidence_type",
        "with_or_from",
        "interacting_taxon_id",
        "annotation_date",
        "assigned_by",
        "limit",
        "offset",
    }
    for method in ("patch", "delete"):
        parameters = paths["/annotations/{annotation_id}"][method]["parameters"]
        assert any(
            parameter["name"] == "If-Match" and parameter["in"] == "header"
            for parameter in parameters
        )


@pytest.mark.parametrize(
    ("path", "method", "expected_statuses"),
    [
        (
            "/annotations",
            "get",
            _openapi_statuses(
                status.HTTP_200_OK,
                status.HTTP_400_BAD_REQUEST,
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
                status.HTTP_400_BAD_REQUEST,
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
                status.HTTP_400_BAD_REQUEST,
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
    assert set(responses) == expected_statuses
    assert "ApiErrorResponse" in schema["components"]["schemas"]
    for status_code, response in responses.items():
        if int(status_code) >= status.HTTP_400_BAD_REQUEST:
            assert response["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ApiErrorResponse"
            }


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
        assert header["description"]


def test_owning_group_has_reusable_field_example(client: TestClient) -> None:
    """SAB-owned group metadata carries a reusable component-level example."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert schemas["AnnotationCreateRequest"]["properties"]["owning_group_id"][
        "examples"
    ] == ["GO_Central"]
    assert schemas["ChangeSetCreateRequest"]["properties"]["owning_group_id"][
        "examples"
    ] == ["GO_Central"]
