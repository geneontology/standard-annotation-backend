"""Documented error responses reference the error envelope and list their codes."""

from standard_annotation_backend.main import app


def _is_error(status_code: str | int) -> bool:
    return str(status_code).startswith(("4", "5"))


def test_every_documented_error_response_lists_its_codes() -> None:
    """Every documented 4xx/5xx response in the schema uses the error envelope.

    This covers the routes that appear in the generated OpenAPI schema,
    including FastAPI's default 422 entry, which must be replaced.
    """
    schema = app.openapi()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for status_code, response in operation.get("responses", {}).items():
                if not _is_error(status_code):
                    continue
                reference = response["content"]["application/json"]["schema"].get(
                    "$ref", ""
                )
                assert reference.endswith("/ApiErrorResponse"), (
                    method,
                    path,
                    status_code,
                )
                assert "`" in response["description"], (method, path, status_code)


def test_every_bearer_route_documents_internal_error() -> None:
    """Every route that accepts a bearer token documents `500: internal_error`."""
    schema = app.openapi()
    checked = []
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if {"Bearer": []} not in operation.get("security", []):
                continue
            checked.append((method, path))
            description = operation["responses"]["500"]["description"]
            assert "`internal_error`" in description, (method, path)
    assert ("get", "/annotations") in checked
    assert ("post", "/change-sets/{change_set_id}/accept") in checked
