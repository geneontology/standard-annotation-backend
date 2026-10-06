"""Shared pytest fixtures for the SAB test suite."""

from collections.abc import Iterator
from urllib.parse import parse_qs

import httpx2
import pytest
from fastapi.testclient import TestClient

from standard_annotation_backend.config import get_settings
from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.main import app


@pytest.fixture
def configured_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide complete test configuration, clearing cached settings between tests."""
    monkeypatch.setenv("SAB_DATABASE_URL", "postgresql+psycopg://sab:sab@db:5432/sab")
    monkeypatch.setenv("SAB_REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("SAB_APPLICATION_SECRET", "test-application-secret")
    monkeypatch.setenv("SAB_ENVIRONMENT", "testing")
    monkeypatch.setenv("SAB_LOG_FORMAT", "console")
    monkeypatch.setenv("SAB_AUTHORIZATION_REFRESH_CRON", "0 0 * * *")
    monkeypatch.setenv("SAB_ONTOLOGY_REFRESH_CRON", "0 2 * * 1,3,5")
    monkeypatch.setenv("SAB_ENTITY_REFRESH_CRON", "0 3 * * *")
    monkeypatch.setenv("SAB_ANNOTATION_REFRESH_CRON", "0 4 * * *")
    monkeypatch.setenv("SAB_GITHUB_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("SAB_GITHUB_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "SAB_GITHUB_OAUTH_CALLBACK_URL", "http://testserver/auth/github/callback"
    )
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def github_http_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> list[httpx2.Response | Exception]:
    """Replace only GitHub HTTP with complete token and profile responses.

    The transport accepts the documented request contract and fails for other
    calls, keeping OAuth parsing and service behavior real in every test.
    """
    payloads: list[dict[str, object]] = [
        {
            "access_token": "test-github-access-token",
            "scope": "",
            "token_type": "bearer",
        },
        {
            "login": "Curator",
            "id": 123,
            "node_id": "MDQ6VXNlcjEyMw==",
            "avatar_url": "https://avatars.githubusercontent.com/u/123?v=4",
            "gravatar_id": "",
            "url": "https://api.github.com/users/Curator",
            "html_url": "https://github.com/Curator",
            "followers_url": "https://api.github.com/users/Curator/followers",
            "following_url": "https://api.github.com/users/Curator/following{/other_user}",
            "gists_url": "https://api.github.com/users/Curator/gists{/gist_id}",
            "starred_url": "https://api.github.com/users/Curator/starred{/owner}{/repo}",
            "subscriptions_url": "https://api.github.com/users/Curator/subscriptions",
            "organizations_url": "https://api.github.com/users/Curator/orgs",
            "repos_url": "https://api.github.com/users/Curator/repos",
            "events_url": "https://api.github.com/users/Curator/events{/privacy}",
            "received_events_url": "https://api.github.com/users/Curator/received_events",
            "type": "User",
            "site_admin": False,
            "name": "Test Curator",
            "company": None,
            "blog": "",
            "location": None,
            "email": None,
            "hireable": None,
            "bio": None,
            "twitter_username": None,
            "public_repos": 1,
            "public_gists": 0,
            "followers": 0,
            "following": 0,
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "private_gists": 0,
            "total_private_repos": 0,
            "owned_private_repos": 0,
            "disk_usage": 0,
            "collaborators": 0,
            "two_factor_authentication": True,
            "plan": {
                "name": "free",
                "space": 976562499,
                "private_repos": 10000,
                "collaborators": 0,
            },
        },
    ]
    responses: list[httpx2.Response | Exception] = []
    for payload in payloads:
        responses.append(httpx2.Response(200, json=payload))

    def handle_request(
        _transport: httpx2.HTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
        assert request.extensions["timeout"] == {
            "connect": 10,
            "read": 10,
            "write": 10,
            "pool": 10,
        }
        if str(request.url) == "https://github.com/login/oauth/access_token":
            assert request.method == "POST"
            assert parse_qs(request.content.decode()) == {
                "client_id": ["test-client-id"],
                "client_secret": ["test-client-secret"],
                "redirect_uri": ["http://testserver/auth/github/callback"],
                "code": ["test-oauth-code"],
            }
            assert request.headers["Accept"] == "application/json"
        else:
            assert str(request.url) == "https://api.github.com/user"
            assert request.method == "GET"
            assert request.headers["Authorization"] == "Bearer test-github-access-token"
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", handle_request)
    return responses


@pytest.fixture
def client(configured_environment: None) -> Iterator[TestClient]:
    """Run the FastAPI application with valid test configuration."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def validated_annotation() -> Annotation:
    """Create a valid annotation for persistence tests.

    Returns:
        A validated annotation containing repeated and unordered list values.
    """
    return Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:P12345",
            "negation": None,
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0008150",
            "references": ["PMID:2", "PMID:1", "PMID:1"],
            "evidence_type": "ECO:0000314",
            "with_or_from": ["UniProtKB:Q2", "UniProtKB:Q1", "UniProtKB:Q1"],
            "interacting_taxon_id": [
                "NCBITaxon:10090",
                "NCBITaxon:9606",
                "NCBITaxon:9606",
            ],
            "annotation_date": "2026-09-09",
            "assigned_by": "GO_Central",
            "annotation_extensions": [
                {
                    "extension_relation": "BFO:0000050",
                    "extension_term": "CL:0000000",
                }
            ],
            "annotation_properties": {"comment": ["excluded"]},
        }
    )
