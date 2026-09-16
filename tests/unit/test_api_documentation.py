"""Test the public API documentation entry points and metadata."""

from importlib.metadata import version as distribution_version

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from standard_annotation_backend.version import get_application_version


def test_application_version_comes_from_installed_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Application metadata uses the version installed by the build backend."""
    monkeypatch.setattr(
        "standard_annotation_backend.version.distribution_version",
        lambda distribution: (
            "1.2.3" if distribution == "standard-annotation-backend" else ""
        ),
    )

    assert get_application_version() == "1.2.3"


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


def test_redoc_is_not_exposed(client: TestClient) -> None:
    """The application does not expose the unused ReDoc interface."""
    response = client.get("/redoc")

    assert response.status_code == status.HTTP_404_NOT_FOUND
