"""Verify the public ontology-load creation contract."""

from fastapi import status
from fastapi.testclient import TestClient


def test_ontology_load_openapi_documents_exact_request_and_response(
    client: TestClient,
) -> None:
    """The route accepts only an ontology key and returns the standard job response."""
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/ontology-loads"]["post"]

    assert operation["security"] == [{"Bearer": []}]
    assert operation["tags"] == ["ontology loads"]
    assert operation["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/OntologyLoadRequest")
    request = schema["components"]["schemas"]["OntologyLoadRequest"]
    assert request["additionalProperties"] is False
    assert request["required"] == ["ontology"]
    assert request["properties"]["ontology"]["$ref"].endswith("/OntologyKey")
    accepted = operation["responses"][str(status.HTTP_202_ACCEPTED)]
    assert accepted["content"]["application/json"]["schema"]["$ref"].endswith(
        "/JobResource"
    )


def test_ontology_load_openapi_documents_typed_errors(client: TestClient) -> None:
    """Every documented failure uses the standard API error response."""
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/ontology-loads"]["post"]["responses"]
    assert set(responses) == {"202", "401", "403", "422", "503"}
    for code in ("401", "403", "422", "503"):
        assert responses[code]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ApiErrorResponse"
        }
