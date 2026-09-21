"""Verify GitHub OAuth at its external HTTP and public error boundaries."""

from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi import status
from fastapi.testclient import TestClient

from standard_annotation_backend.auth.github_oauth import (
    GitHubOAuthClient,
    OAuthConfigurationError,
    OAuthUpstreamError,
)
from standard_annotation_backend.config import Settings
from standard_annotation_backend.main import app


def test_authorization_requests_only_identity_and_binds_state_and_callback(
    configured_environment: None,
) -> None:
    """Authorization includes unpredictable state and the configured callback URL."""
    parsed = urlsplit(GitHubOAuthClient(Settings()).authorization_url("test-state"))
    assert parsed.scheme == "https"
    assert parsed.netloc == "github.com"
    assert parsed.path == "/login/oauth/authorize"
    assert parse_qs(parsed.query, keep_blank_values=True) == {
        "client_id": ["test-client-id"],
        "redirect_uri": ["http://testserver/auth/github/callback"],
        "state": ["test-state"],
        "scope": [""],
    }


def test_code_exchange_validates_the_authenticated_github_profile(
    configured_environment: None,
    github_http_responses: list[httpx2.Response | Exception],
) -> None:
    """The exchanged bearer credential maps the freshly verified GitHub login."""
    client = GitHubOAuthClient(Settings())
    token = client.exchange_code("test-oauth-code")
    assert token == "test-github-access-token"
    profile = github_http_responses[0]
    assert isinstance(profile, httpx2.Response)
    github_http_responses[0] = httpx2.Response(
        200, json={"login": "curator", "name": "Test Curator"}
    )
    identity = client.get_user(token)
    assert identity.github_login == "curator"
    assert identity.display_name == "Test Curator"


@pytest.mark.parametrize(
    "failure", ["http", "redirect", "error", "malformed", "timeout"]
)
def test_upstream_exchange_failures_have_safe_stable_errors(
    configured_environment: None,
    github_http_responses: list[httpx2.Response | Exception],
    failure: str,
) -> None:
    """GitHub failures never expose the code, credential, or upstream error body."""
    response = httpx2.Response(
        503 if failure == "http" else 302 if failure == "redirect" else 200,
        content=(
            b'{"error":"test-oauth-code test-client-secret"}'
            if failure == "error"
            else b"test-oauth-code test-client-secret"
        ),
    )
    github_http_responses[:] = [
        httpx2.TimeoutException("test-oauth-code test-client-secret")
        if failure == "timeout"
        else response
    ]
    with pytest.raises(OAuthUpstreamError) as raised:
        GitHubOAuthClient(Settings()).exchange_code("test-oauth-code")
    assert str(raised.value) == "GitHub authentication is unavailable"
    assert raised.value.__suppress_context__


@pytest.mark.parametrize("payload", [b"{}", b'{"login":""}', b'{"login":true}'])
def test_github_profile_requires_a_real_identity(
    configured_environment: None,
    github_http_responses: list[httpx2.Response | Exception],
    payload: bytes,
) -> None:
    """Incomplete or invalid upstream profiles cannot identify a synchronized user."""
    github_http_responses[:] = [httpx2.Response(200, content=payload)]
    with pytest.raises(OAuthUpstreamError):
        GitHubOAuthClient(Settings()).get_user("test-github-access-token")


@pytest.mark.parametrize(
    "setting",
    [
        "github_oauth_client_id",
        "github_oauth_client_secret",
        "github_oauth_callback_url",
    ],
)
def test_oauth_configuration_is_checked_when_used(
    configured_environment: None, setting: str
) -> None:
    """Missing OAuth settings leave startup usable and fail only OAuth operations."""
    settings = Settings().model_copy(update={setting: None})
    with pytest.raises(OAuthConfigurationError):
        GitHubOAuthClient(settings)


def test_login_sets_distinct_short_lived_http_only_state(client: TestClient) -> None:
    """Each login binds a fresh state to a short-lived cookie before redirecting."""
    first = client.get("/auth/github/login", follow_redirects=False)
    assert first.status_code == status.HTTP_302_FOUND
    state = parse_qs(urlsplit(first.headers["location"]).query)["state"][0]
    assert client.cookies["sab_oauth_state"] == state
    cookie = first.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Path=/" in cookie and "Max-Age=600" in cookie
    assert first.headers["cache-control"] == "no-store"
    second = client.get("/auth/github/login", follow_redirects=False)
    assert parse_qs(urlsplit(second.headers["location"]).query)["state"][0] != state


@pytest.mark.parametrize("state", [None, "wrong-state", "\N{SNOWMAN}"])
def test_callback_rejects_state_mismatch_and_clears_cookie(
    client: TestClient, state: str | None
) -> None:
    """A callback without the browser's exact state cannot create a session."""
    client.get("/auth/github/login", follow_redirects=False)
    params = {"code": "test-oauth-code"}
    if state is not None:
        params["state"] = state
    response = client.get("/auth/github/callback", params=params)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_oauth_state"
    assert "sab_oauth_state" not in client.cookies
    assert "sab_token_management_session" not in client.cookies


def test_unconfigured_oauth_login_keeps_health_available(client: TestClient) -> None:
    """An unconfigured OAuth deployment returns a stable login error and healthy status."""
    app.state.settings.github_oauth_client_secret = None
    assert client.get("/health").status_code == status.HTTP_200_OK
    response = client.get("/auth/github/login", follow_redirects=False)
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["error"]["code"] == "oauth_not_configured"


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_deployed_login_cookie_requires_https(
    client: TestClient, environment: str
) -> None:
    """Nonlocal deployments issue Secure state cookies and require an HTTPS callback."""
    app.state.settings.environment = environment
    response = client.get("/auth/github/login", follow_redirects=False)
    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    app.state.settings.github_oauth_callback_url = (
        "https://testserver/auth/github/callback"
    )
    response = client.get("/auth/github/login", follow_redirects=False)
    assert response.status_code == status.HTTP_302_FOUND
    assert "Secure" in response.headers["set-cookie"]


@pytest.mark.parametrize("query", [{}, {"error": "access_denied"}])
def test_incomplete_oauth_callback_clears_valid_state(
    client: TestClient, query: dict[str, str]
) -> None:
    """Denied or incomplete sign-in ends the state flow without issuing a session."""
    client.get("/auth/github/login", follow_redirects=False)
    query = {**query, "state": client.cookies["sab_oauth_state"]}
    response = client.get("/auth/github/callback", params=query, follow_redirects=False)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_oauth_callback"
    assert "sab_oauth_state" not in client.cookies


def test_github_failure_clears_callback_state_and_uses_public_error(
    client: TestClient,
    github_http_responses: list[httpx2.Response | Exception],
) -> None:
    """An upstream outage has a safe typed response and consumes the browser state."""
    client.get("/auth/github/login", follow_redirects=False)
    state = client.cookies["sab_oauth_state"]
    github_http_responses[:] = [
        httpx2.TimeoutException("test-oauth-code test-client-secret")
    ]
    response = client.get(
        "/auth/github/callback", params={"state": state, "code": "test-oauth-code"}
    )
    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()["error"]["code"] == "github_oauth_unavailable"
    assert "test-oauth-code" not in response.text
    assert "test-client-secret" not in response.text
    assert "sab_oauth_state" not in client.cookies
