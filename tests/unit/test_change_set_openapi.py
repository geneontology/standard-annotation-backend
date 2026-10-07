"""Verify the public proposal and review OpenAPI contract."""

import pytest
from fastapi import status
from fastapi.testclient import TestClient


def test_proposal_ownership_is_optional_and_documents_token_derivation(
    client: TestClient,
) -> None:
    """Create proposals accept ownership derived from a selected restricted group."""
    schema = client.get("/openapi.json").json()["components"]["schemas"][
        "ChangeSetCreateRequest"
    ]
    assert "owning_group_id" not in schema["required"]
    description = schema["properties"]["owning_group_id"]["description"]
    assert "global" in description and "selected group" in description


def test_proposal_route_includes_create_update_and_delete_examples(
    client: TestClient,
) -> None:
    """The proposal body documents each discriminated change-set workflow."""
    content = client.get("/openapi.json").json()["paths"]["/change-sets"]["post"][
        "requestBody"
    ]["content"]["application/json"]
    examples = content["examples"]

    assert set(examples) == {"create", "update", "delete"}
    assert examples["create"]["value"] == {
        "operation": "create",
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
        "reason": "Add annotation supported by the cited publication",
    }
    assert examples["update"]["value"] == {
        "operation": "update",
        "annotation_id": "018f6fa8-709b-7c13-9f75-4513f57b98c2",
        "base_version": 3,
        "patch_format": "application/json-patch+json",
        "patch": [
            {
                "op": "replace",
                "path": "/references",
                "value": ["PMID:12345678", "GO_REF:0000002"],
            }
        ],
        "reason": "Add the supporting GO reference",
    }
    assert examples["delete"]["value"] == {
        "operation": "delete",
        "annotation_id": "018f6fa8-709b-7c13-9f75-4513f57b98c2",
        "base_version": 3,
        "reason": "Withdraw an annotation that is no longer supported",
    }


def test_review_routes_include_acceptance_and_rejection_examples(
    client: TestClient,
) -> None:
    """Review operations show meaningful optional and required reviewer text."""
    paths = client.get("/openapi.json").json()["paths"]
    accept = paths["/change-sets/{change_set_id}/accept"]["post"]["requestBody"]
    reject = paths["/change-sets/{change_set_id}/reject"]["post"]["requestBody"]

    accept_examples = accept["content"]["application/json"]["examples"]
    reject_examples = reject["content"]["application/json"]["examples"]
    assert set(accept_examples) == {"accept"}
    assert accept_examples["accept"]["value"] == {
        "review_reason": "Verified against the cited source evidence"
    }
    assert set(reject_examples) == {"reject"}
    assert reject_examples["reject"]["value"] == {
        "review_reason": "The evidence does not support the proposed change"
    }
    assert accept.get("required") is not True
    assert reject["required"] is True


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
    errors = errors | {
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    assert set(responses) == {str(code) for code in {success, *errors}}
    assert schema["paths"][path][method]["security"] == [{"Bearer": []}]
    assert (
        responses["401"]["headers"]["WWW-Authenticate"]["schema"]["const"] == "Bearer"
    )
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


def test_change_set_responses_share_operation_and_state_vocabularies(
    client: TestClient,
) -> None:
    """Responses reference one named schema for each change-set vocabulary."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    operation_ref = {"$ref": "#/components/schemas/ChangeSetOperation"}
    assert schemas["ChangeSetResource"]["properties"]["operation"] == operation_ref
    assert (
        schemas["ChangeSetPreviewResource"]["properties"]["operation"] == operation_ref
    )
    assert schemas["ChangeSetResource"]["properties"]["state"] == {
        "$ref": "#/components/schemas/ChangeSetState"
    }
    assert schemas["ChangeSetOperation"]["enum"] == ["create", "update", "delete"]
    assert schemas["ChangeSetState"]["enum"] == [
        "proposed",
        "accepted",
        "rejected",
        "stale",
    ]


@pytest.mark.parametrize(
    ("path", "method", "documented"),
    [
        (
            "/change-sets",
            "post",
            {
                "404": {"annotation_not_found"},
                "422": {"invalid_change_set", "request_validation_error"},
            },
        ),
        (
            "/change-sets/{change_set_id}",
            "get",
            {
                "404": {"change_set_not_found"},
                "422": {"request_validation_error"},
            },
        ),
        (
            "/change-sets/{change_set_id}/preview",
            "post",
            {
                "404": {"change_set_not_found"},
                "409": {"change_set_not_proposed"},
                "422": {"request_validation_error"},
            },
        ),
        (
            "/change-sets/{change_set_id}/accept",
            "post",
            {
                "404": {"change_set_not_found"},
                "409": {
                    "change_set_not_proposed",
                    "stale_change_set",
                    "duplicate_annotation",
                },
                "422": {
                    "invalid_change_set",
                    "unknown_db_object_id",
                    "request_validation_error",
                },
            },
        ),
        (
            "/change-sets/{change_set_id}/reject",
            "post",
            {
                "404": {"change_set_not_found"},
                "409": {"change_set_not_proposed"},
                "422": {"invalid_change_set", "request_validation_error"},
            },
        ),
    ],
)
def test_change_set_error_docs_list_their_codes(
    client: TestClient, path: str, method: str, documented: dict[str, set[str]]
) -> None:
    """Each change-set error status documents every code the route can return."""
    responses = client.get("/openapi.json").json()["paths"][path][method]["responses"]

    for status_code, codes in documented.items():
        description = responses[status_code]["description"]
        for code in codes:
            assert f"`{code}`" in description
