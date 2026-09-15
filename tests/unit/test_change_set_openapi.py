"""Verify the public proposal and review OpenAPI contract."""

import pytest
from fastapi import status
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    ("path", "method", "success", "model", "errors"),
    [
        (
            "/change-sets",
            "post",
            status.HTTP_201_CREATED,
            "ChangeSetResource",
            {status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT},
        ),
        (
            "/change-sets/{change_set_id}",
            "get",
            status.HTTP_200_OK,
            "ChangeSetResource",
            {status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT},
        ),
        (
            "/change-sets/{change_set_id}/preview",
            "post",
            status.HTTP_200_OK,
            "ChangeSetPreviewResource",
            {
                status.HTTP_404_NOT_FOUND,
                status.HTTP_409_CONFLICT,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            },
        ),
        (
            "/change-sets/{change_set_id}/accept",
            "post",
            status.HTTP_200_OK,
            "AcceptedChangeSetResource",
            {
                status.HTTP_404_NOT_FOUND,
                status.HTTP_409_CONFLICT,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            },
        ),
        (
            "/change-sets/{change_set_id}/reject",
            "post",
            status.HTTP_200_OK,
            "ChangeSetResource",
            {
                status.HTTP_404_NOT_FOUND,
                status.HTTP_409_CONFLICT,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            },
        ),
    ],
)
def test_change_set_routes_and_responses(
    client: TestClient,
    path: str,
    method: str,
    success: int,
    model: str,
    errors: set[int],
) -> None:
    """Each route advertises exactly its success model and applicable errors."""
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"][path]) == {method}
    responses = schema["paths"][path][method]["responses"]
    assert set(responses) == {str(code) for code in {success, *errors}}
    assert responses[str(success)]["content"]["application/json"]["schema"] == {
        "$ref": f"#/components/schemas/{model}"
    }
    for code in errors:
        assert responses[str(code)]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ApiErrorResponse"
        }


def test_proposal_discriminator_and_raw_candidates(client: TestClient) -> None:
    """The proposal union preserves raw candidates and distinguishes operation fields."""
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/change-sets"]["post"]
    body = operation["requestBody"]
    assert body["required"] is True
    union = body["content"]["application/json"]["schema"]
    assert union["discriminator"]["propertyName"] == "operation"
    assert set(union["discriminator"]["mapping"]) == {"create", "update", "delete"}
    assert len(union["oneOf"]) == 3
    schemas = schema["components"]["schemas"]
    for operation_name, ref in union["discriminator"]["mapping"].items():
        model = schemas[ref.rsplit("/", 1)[1]]
        assert model["additionalProperties"] is False
        assert model["properties"]["operation"]["const"] == operation_name
        assert {"reason", "operation"} <= set(model["required"])
        if operation_name == "create":
            assert set(model["required"]) == {
                "operation",
                "reason",
                "owning_group_id",
                "annotation",
            }
            assert model["properties"]["annotation"]["type"] == "object"
            assert "$ref" not in model["properties"]["annotation"]
        else:
            assert {"annotation_id", "base_version"} <= set(model["required"])
            assert model["properties"]["base_version"]["exclusiveMinimum"] == 0
            if operation_name == "update":
                assert "patch" in model["required"]
                assert model["properties"]["patch"]["minItems"] == 1
                assert "patch_format" not in model["required"]
                assert model["properties"]["patch_format"]["const"] == (
                    "application/json-patch+json"
                )
                assert model["properties"]["patch_format"]["default"] == (
                    "application/json-patch+json"
                )
    headers = operation["responses"][str(status.HTTP_201_CREATED)]["headers"]
    assert set(headers) == {"Location"}
    assert headers["Location"]["schema"]["type"] == "string"
    assert headers["Location"]["description"]


def test_review_request_models_and_stored_preview(client: TestClient) -> None:
    """Review bodies document optional acceptance text and required rejection text."""
    schema = client.get("/openapi.json").json()
    schemas = schema["components"]["schemas"]
    for action, model in [
        ("accept", "ChangeSetAcceptRequest"),
        ("reject", "ChangeSetRejectRequest"),
    ]:
        body = schema["paths"][f"/change-sets/{{change_set_id}}/{action}"]["post"][
            "requestBody"
        ]
        assert model in str(body["content"]["application/json"]["schema"])
        assert schemas[model]["additionalProperties"] is False
    assert "review_reason" in schemas["ChangeSetRejectRequest"]["required"]
    assert "review_reason" not in schemas["ChangeSetAcceptRequest"].get("required", [])
    assert "ChangeSetPreviewResource" in str(
        schemas["ChangeSetResource"]["properties"]["preview"]
    )
    assert "ApiValidationIssue" in str(
        schemas["ChangeSetPreviewResource"]["properties"]["validation_errors"]
    )
    assert {
        "change_set_id",
        "annotation_id",
        "expected_version",
        "current_version",
    } == set(schemas["StaleChangeSetDetails"]["required"])
