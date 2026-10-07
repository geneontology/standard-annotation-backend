"""Verify that token-management implementation routes are not a public contract."""

import pytest
from fastapi import status
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from standard_annotation_backend.api.routes.tokens import router


def test_token_management_routes_are_hidden_from_the_public_contract(
    client: TestClient,
) -> None:
    """Internal token-management routes and cookies stay out of OpenAPI."""
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert (
        not {
            "/auth/github/login",
            "/auth/github/callback",
            "/token-management",
            "/assets/favicon.svg",
            "/assets/token-management.css",
            "/assets/token-management.js",
            "/tokens",
            "/tokens/contexts",
            "/tokens/{token_id}",
        }
        & paths.keys()
    )
    assert "TokenManagementSession" not in schema["components"]["securitySchemes"]


def _route(path: str, method: str) -> APIRoute:
    """Find a token-management route; they are absent from the public schema."""
    return next(
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in (route.methods or ())
    )


def _error_descriptions(path: str, method: str) -> dict[int, str]:
    """Return each documented error status of a route with its description."""
    return {
        status_code: response.get("description", "")
        for status_code, response in _route(path, method).responses.items()
        if isinstance(status_code, int) and status_code >= status.HTTP_400_BAD_REQUEST
    }


@pytest.mark.parametrize(
    ("path", "method"), [("/tokens", "POST"), ("/tokens/{token_id}", "DELETE")]
)
def test_token_mutations_document_their_csrf_rejection(path: str, method: str) -> None:
    """Routes that enforce CSRF document the 403 `invalid_csrf_token` outcome."""
    responses = _error_descriptions(path, method)

    assert "`invalid_csrf_token`" in responses[status.HTTP_403_FORBIDDEN]
    assert "`management_session_required`" in responses[status.HTTP_401_UNAUTHORIZED]
    assert (
        "`credential_storage_unavailable`"
        in responses[status.HTTP_503_SERVICE_UNAVAILABLE]
    )


def test_token_creation_documents_rejected_input() -> None:
    """Token creation documents both kinds of invalid input it can reject."""
    description = _error_descriptions("/tokens", "POST")[
        status.HTTP_422_UNPROCESSABLE_CONTENT
    ]

    assert "`invalid_token`" in description
    assert "`request_validation_error`" in description


def test_oauth_callback_documents_only_errors_it_can_return() -> None:
    """The OAuth callback documents its failures and no request-validation 422."""
    responses = _error_descriptions("/auth/github/callback", "GET")

    assert set(responses) == {
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    assert "`invalid_oauth_state`" in responses[status.HTTP_400_BAD_REQUEST]
    assert "`invalid_oauth_callback`" in responses[status.HTTP_400_BAD_REQUEST]
    assert "`github_identity_not_allowed`" in responses[status.HTTP_403_FORBIDDEN]
    assert "`github_oauth_unavailable`" in responses[status.HTTP_502_BAD_GATEWAY]
    assert "`oauth_not_configured`" in responses[status.HTTP_503_SERVICE_UNAVAILABLE]
