"""Verify shared GitHub request and response handling."""

import httpx2
import pytest

from standard_annotation_backend.integrations.github import (
    GitHubClient,
    GitHubIntegrationError,
)

SHA = "a" * 40


def _client(
    responses: list[httpx2.Response | Exception],
) -> tuple[GitHubClient, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )

    http_client = httpx2.Client(transport=httpx2.MockTransport(handler))
    return GitHubClient(client=http_client), requests


def test_resolve_commit_applies_api_policy_and_normalizes_sha() -> None:
    """Commit resolution sends versioned headers and returns a lowercase SHA."""
    github, requests = _client([httpx2.Response(200, json={"sha": SHA.upper()})])

    commit_sha = github.resolve_commit("geneontology/go-ontology", "master")

    assert commit_sha == SHA
    assert len(requests) == 1
    assert str(requests[0].url) == (
        "https://api.github.com/repos/geneontology/go-ontology/commits/master"
    )
    assert requests[0].headers["Accept"] == "application/vnd.github+json"
    assert requests[0].headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_fetch_raw_content_uses_exact_commit() -> None:
    """Raw retrieval never addresses content through a mutable ref."""
    github, requests = _client([httpx2.Response(200, content=b"ontology")])

    content = github.fetch_raw_content(
        "geneontology/go-ontology",
        "src/ontology/go-edit.obo",
        SHA.upper(),
    )

    assert content == b"ontology"
    assert len(requests) == 1
    assert str(requests[0].url) == (
        "https://raw.githubusercontent.com/geneontology/go-ontology/"
        f"{SHA}/src/ontology/go-edit.obo"
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(404, content=b"private response details"),
        httpx2.Response(200, content=b"not-json"),
        httpx2.Response(200, json={"sha": "a" * 39}),
    ],
)
def test_resolve_commit_translates_unusable_responses(
    response: httpx2.Response,
) -> None:
    """Callers receive one GitHub error without upstream response details."""
    github, _requests = _client([response])

    with pytest.raises(GitHubIntegrationError) as caught:
        github.resolve_commit("geneontology/go-ontology", "master")

    assert "private" not in str(caught.value)


def test_content_retrieval_rejects_invalid_commit_before_request() -> None:
    """Content cannot be requested through a non-immutable revision."""
    github, requests = _client([])

    with pytest.raises(GitHubIntegrationError):
        github.fetch_raw_content("owner/repository", "ontology.obo", "master")

    assert requests == []
