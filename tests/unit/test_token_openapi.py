"""Verify that token-management implementation routes are not a public contract."""

from fastapi.testclient import TestClient


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
            "/tokens",
            "/tokens/contexts",
            "/tokens/{token_id}",
        }
        & paths.keys()
    )
    assert "TokenManagementSession" not in schema["components"]["securitySchemes"]
