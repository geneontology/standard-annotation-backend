"""Verify retrieval of GitHub and HTTPS sources with shared provenance."""

from hashlib import sha256

import httpx2
import pytest

from standard_annotation_backend.domain.refresh import RefreshFailureCode
from standard_annotation_backend.refresh.fetchers import SourceError, SourceFetchers
from standard_annotation_backend.refresh.sources import GitHubSource, HttpsSource

SHA = "A" * 40
GITHUB = GitHubSource(
    type="github",
    repository="geneontology/go-site",
    ref="master",
    path="metadata/users.yaml",
)
HTTPS = HttpsSource(type="https", url="https://example.org/mgi.gpi.gz")


def _fetchers(handler: httpx2.MockTransport) -> SourceFetchers:
    return SourceFetchers(
        httpx2.Client(transport=handler),
        connect_timeout_seconds=3,
        read_timeout_seconds=7,
    )


def test_github_source_resolves_ref_and_records_commit() -> None:
    """GitHub fetches resolve the ref and download the path at that commit."""
    requests: list[httpx2.Request] = []
    body = b"- accounts: {github: curator}\n"

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.host == "api.github.com":
            return httpx2.Response(200, json={"sha": SHA})
        return httpx2.Response(200, content=body)

    document = _fetchers(httpx2.MockTransport(handle)).fetch("go-site", GITHUB)

    assert [str(request.url) for request in requests] == [
        "https://api.github.com/repos/geneontology/go-site/commits/master",
        f"https://raw.githubusercontent.com/geneontology/go-site/{SHA.lower()}"
        "/metadata/users.yaml",
    ]
    assert requests[0].extensions["timeout"] == {
        "connect": 3,
        "read": 7,
        "write": 7,
        "pool": 7,
    }
    assert document.source_key == "go-site"
    assert document.source_type == "github"
    assert document.source_locator == "github:geneontology/go-site:metadata/users.yaml"
    assert document.source_revision == SHA.lower()
    assert document.source_checksum == sha256(body).hexdigest()
    assert document.content == body
    assert document.fetched_at.tzinfo is not None
    assert document.provenance.source_revision == SHA.lower()


def test_https_source_follows_redirects_and_keeps_configured_locator() -> None:
    """HTTPS provenance names the configured URL even after a redirect."""
    body = b"\x1f\x8bcompressed"

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/mgi.gpi.gz":
            return httpx2.Response(302, headers={"Location": "/moved.gpi.gz"})
        return httpx2.Response(200, content=body)

    document = _fetchers(httpx2.MockTransport(handle)).fetch("mgi", HTTPS)

    assert document.source_type == "https"
    assert document.source_locator == "https://example.org/mgi.gpi.gz"
    assert document.source_revision is None
    assert document.source_checksum == sha256(body).hexdigest()
    assert document.content == body


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (httpx2.Response(404), RefreshFailureCode.HTTP_STATUS),
        (httpx2.Response(503), RefreshFailureCode.HTTP_STATUS),
        (httpx2.ReadTimeout("slow"), RefreshFailureCode.TIMEOUT),
        (httpx2.ConnectError("refused"), RefreshFailureCode.SOURCE_ERROR),
    ],
)
def test_https_failures_have_stable_codes_without_urls(
    outcome: httpx2.Response | Exception, code: RefreshFailureCode
) -> None:
    """Retrieval failures report a fixed code and never the URL."""

    def handle(_request: httpx2.Request) -> httpx2.Response:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with pytest.raises(SourceError) as raised:
        _fetchers(httpx2.MockTransport(handle)).fetch("mgi", HTTPS)

    assert raised.value.code is code
    assert "example.org" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_https_redirect_loop_stops_with_source_error() -> None:
    """A redirect loop stops instead of hanging."""
    loop = httpx2.MockTransport(
        lambda _request: httpx2.Response(302, headers={"Location": "/mgi.gpi.gz"})
    )

    with pytest.raises(SourceError) as raised:
        _fetchers(loop).fetch("mgi", HTTPS)

    assert raised.value.code is RefreshFailureCode.SOURCE_ERROR


def test_https_redirect_to_plain_http_is_rejected() -> None:
    """A redirect that downgrades to `http` fails instead of returning the body."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.scheme == "https":
            return httpx2.Response(302, headers={"Location": "http://example.org/x"})
        return httpx2.Response(200, content=b"tampered")

    with pytest.raises(SourceError) as raised:
        _fetchers(httpx2.MockTransport(handle)).fetch("mgi", HTTPS)

    assert raised.value.code is RefreshFailureCode.SOURCE_ERROR
    assert "example.org" not in str(raised.value)


def test_github_connection_failure_reports_source_error() -> None:
    """A transport error while talking to GitHub is a `source_error`."""

    def handle(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    with pytest.raises(SourceError) as raised:
        _fetchers(httpx2.MockTransport(handle)).fetch("go-site", GITHUB)

    assert raised.value.code is RefreshFailureCode.SOURCE_ERROR


@pytest.mark.parametrize("status", [404, 500])
def test_github_failures_report_source_error(status: int) -> None:
    """Any unusable GitHub response is a `source_error`."""
    transport = httpx2.MockTransport(lambda _request: httpx2.Response(status))

    with pytest.raises(SourceError) as raised:
        _fetchers(transport).fetch("go-site", GITHUB)

    assert raised.value.code is RefreshFailureCode.SOURCE_ERROR
