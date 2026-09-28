"""Verify immutable retrieval of the configured authorization source."""

import httpx2
import pytest

from standard_annotation_backend.auth.authorization_source import (
    AuthorizationSourceClient,
    AuthorizationSourceUnavailableError,
)

SHA = "a" * 40


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[httpx2.Response | Exception],
) -> list[httpx2.Request]:
    requests: list[httpx2.Request] = []

    def handle_request(
        _transport: httpx2.HTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
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

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", handle_request)
    return requests


def test_fetch_resolves_ref_then_reads_source_at_immutable_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source document is fetched by the exact resolved commit, not a mutable ref."""
    requests = _install_transport(
        monkeypatch,
        [
            httpx2.Response(200, json={"sha": SHA}),
            httpx2.Response(200, content=b"- accounts: {github: curator}\n"),
        ],
    )

    document = AuthorizationSourceClient(
        repository="geneontology/go-site",
        ref="release/test",
        path="metadata/users.yaml",
    ).fetch()

    assert document.yaml_text == "- accounts: {github: curator}\n"
    assert document.source_repository == "geneontology/go-site"
    assert document.source_commit_sha == SHA
    assert len(requests) == 2
    assert str(requests[0].url) == (
        "https://api.github.com/repos/geneontology/go-site/commits/release/test"
    )
    assert str(requests[1].url).split("?")[0] == (
        "https://api.github.com/repos/geneontology/go-site/contents/metadata/users.yaml"
    )
    assert requests[1].url.params["ref"] == SHA
    assert requests[0].headers["Accept"] == "application/vnd.github+json"
    assert requests[1].headers["Accept"] == "application/vnd.github.raw+json"


def test_fetch_at_commit_skips_mutable_ref_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovered job can retrieve its previously persisted immutable revision."""
    requests = _install_transport(
        monkeypatch,
        [httpx2.Response(200, content=b"[]\n")],
    )

    document = AuthorizationSourceClient(
        repository="geneontology/go-site",
        ref="master",
        path="metadata/users.yaml",
    ).fetch_at_commit(SHA)

    assert document.source_commit_sha == SHA
    assert len(requests) == 1
    assert "/contents/metadata/users.yaml" in str(requests[0].url)
    assert requests[0].url.params["ref"] == SHA


@pytest.mark.parametrize(
    "responses",
    [
        [httpx2.Response(404, content=b"secret upstream details")],
        [httpx2.Response(302, headers={"Location": "https://evil.example/commit"})],
        [httpx2.Response(200, content=b"not-json")],
        [httpx2.Response(200, json={"sha": "a" * 39})],
        [httpx2.Response(200, json={"sha": "A" * 40})],
        [
            httpx2.Response(200, json={"sha": SHA}),
            httpx2.Response(503, content=b"private response body"),
        ],
        [
            httpx2.Response(200, json={"sha": SHA}),
            httpx2.Response(307, headers={"Location": "https://evil.example/source"}),
        ],
        [
            httpx2.Response(200, json={"sha": SHA}),
            httpx2.Response(200, content=b"\xff"),
        ],
    ],
)
def test_fetch_returns_one_safe_error_for_invalid_upstream_responses(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[httpx2.Response],
) -> None:
    """Invalid GitHub responses produce one error without response details."""
    _install_transport(monkeypatch, list(responses))

    with pytest.raises(
        AuthorizationSourceUnavailableError,
        match=r"^go-site users\.yaml is unavailable$",
    ) as caught:
        AuthorizationSourceClient(
            repository="geneontology/go-site",
            ref="master",
            path="metadata/users.yaml",
        ).fetch()

    assert "secret" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_fetch_returns_safe_error_for_request_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public source error does not include network error details."""
    request = httpx2.Request("GET", "https://api.github.com")
    _install_transport(
        monkeypatch,
        [httpx2.ConnectError("credential-like diagnostic", request=request)],
    )

    with pytest.raises(
        AuthorizationSourceUnavailableError,
        match=r"^go-site users\.yaml is unavailable$",
    ) as caught:
        AuthorizationSourceClient(
            repository="geneontology/go-site",
            ref="master",
            path="metadata/users.yaml",
        ).fetch()

    assert "credential-like" not in str(caught.value)


@pytest.mark.parametrize("sha", ["a" * 39, "A" * 40, "not-a-sha"])
def test_fetch_at_commit_rejects_noncanonical_sha(sha: str) -> None:
    """Recovery accepts only a full lowercase immutable commit identifier."""
    with pytest.raises(
        AuthorizationSourceUnavailableError,
        match=r"^go-site users\.yaml is unavailable$",
    ):
        AuthorizationSourceClient(
            repository="geneontology/go-site",
            ref="master",
            path="metadata/users.yaml",
        ).fetch_at_commit(sha)
