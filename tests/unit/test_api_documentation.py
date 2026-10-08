"""Test the public API documentation entry points and metadata."""

from importlib.metadata import version as distribution_version

from fastapi import status
from fastapi.testclient import TestClient


def test_openapi_displays_installed_application_version(client: TestClient) -> None:
    """OpenAPI and Swagger identify the version installed in this environment."""
    response = client.get("/openapi.json")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["info"]["version"] == distribution_version(
        "standard-annotation-backend"
    )


def test_swagger_ui_remains_available(client: TestClient) -> None:
    """The application continues to expose its supported Swagger interface."""
    response = client.get("/docs")

    assert response.status_code == status.HTTP_200_OK
    assert "Swagger UI" in response.text
    assert '<link rel="shortcut icon" href="/assets/favicon.svg">' in response.text


def test_redoc_is_not_exposed(client: TestClient) -> None:
    """The application does not expose the unused ReDoc interface."""
    response = client.get("/redoc")

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_bearer_security_applies_only_to_data_operations(client: TestClient) -> None:
    """Data routes advertise bearer authentication while public and cookie routes do not."""
    schema = client.get("/openapi.json").json()
    bearer = schema["components"]["securitySchemes"]["Bearer"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    assert "security" not in schema
    for path, operations in schema["paths"].items():
        for operation in operations.values():
            if path.startswith(
                (
                    "/admin",
                    "/annotations",
                    "/change-sets",
                )
            ):
                assert operation["security"] == [{"Bearer": []}]
            else:
                assert {"Bearer": []} not in operation.get("security", [])
