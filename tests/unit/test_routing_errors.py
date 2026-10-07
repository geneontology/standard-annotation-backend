"""Tests for routing failures and other HTTP exceptions using the error envelope."""

from fastapi import FastAPI, status
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from standard_annotation_backend.api.errors import install_exception_handlers
from standard_annotation_backend.main import app


def test_unknown_route_returns_route_not_found_envelope() -> None:
    """A path that matches no route returns a JSON `route_not_found` error."""
    response = TestClient(app).get("/no-such-route")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {
        "error": {"code": "route_not_found", "message": "Route was not found"}
    }


def test_unsupported_method_returns_method_not_allowed_with_allow_header() -> None:
    """A method a path does not support returns `method_not_allowed` and `Allow`."""
    response = TestClient(app).put("/annotations")

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert response.json() == {
        "error": {"code": "method_not_allowed", "message": "Method is not allowed"}
    }
    allowed = {method.strip() for method in response.headers["Allow"].split(",")}
    assert allowed == {"GET", "POST"}


def test_allow_lists_methods_of_every_route_serving_the_path() -> None:
    """`Allow` names every method the path supports, across separate routes."""
    response = TestClient(app).post("/annotations/00000000-0000-0000-0000-000000000001")

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    allowed = {method.strip() for method in response.headers["Allow"].split(",")}
    assert allowed == {"GET", "PATCH", "DELETE"}


def test_static_asset_method_errors_never_send_an_empty_allow_header() -> None:
    """A method error from the static files keeps the envelope and no empty `Allow`."""
    response = TestClient(app).put("/assets/token-management.js")

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert response.headers.get("Allow") != ""


def test_other_http_exceptions_become_internal_errors() -> None:
    """An HTTP exception SAB does not map returns the generic `internal_error`."""
    other = FastAPI()
    install_exception_handlers(other)

    @other.get("/teapot")
    def teapot() -> None:
        raise HTTPException(status_code=status.HTTP_418_IM_A_TEAPOT, detail="secret")

    response = TestClient(other).get("/teapot")

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An unexpected error occurred"}
    }
