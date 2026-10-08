"""Test the generated OpenAPI contract for annotation comments."""

from fastapi import status
from fastapi.testclient import TestClient


def _status_codes(*values: int) -> set[str]:
    return {str(value) for value in values}


def test_comment_openapi_documents_routes_models_and_errors(
    client: TestClient,
) -> None:
    """Comment operations expose their routes, response models, and documented errors."""
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    collection = paths["/annotations/{annotation_id}/comments"]
    item = paths["/annotations/{annotation_id}/comments/{comment_id}"]

    assert set(collection) == {"get", "post"}
    assert set(item) == {"patch", "delete"}
    assert collection["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/AnnotationCommentPageResponse")
    assert collection["post"]["responses"]["201"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/AnnotationCommentResource")
    location = collection["post"]["responses"]["201"]["headers"]["Location"]
    assert location["schema"]["type"] == "string"
    assert item["patch"]["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/AnnotationCommentResource")
    assert "content" not in item["delete"]["responses"]["204"]

    common_errors = _status_codes(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    )
    assert set(collection["get"]["responses"]) == {"200"} | common_errors
    assert set(collection["post"]["responses"]) == {"201"} | common_errors
    assert set(item["patch"]["responses"]) == {"200"} | common_errors
    assert set(item["delete"]["responses"]) == {"204"} | common_errors
    for operation in collection.values():
        description = operation["responses"]["404"]["description"]
        assert "`annotation_not_found`" in description
        assert "comment_not_found" not in description
    for operation in item.values():
        description = operation["responses"]["404"]["description"]
        assert "`annotation_not_found`" in description
        assert "`comment_not_found`" in description

    request_schema = schema["components"]["schemas"]["AnnotationCommentRequest"]
    assert request_schema["required"] == ["body"]
    assert set(request_schema["properties"]) == {"body"}
    assert request_schema["additionalProperties"] is False
