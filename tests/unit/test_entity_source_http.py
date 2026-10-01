"""Verify retrieval of configured entity source files."""

import gzip
from hashlib import sha256

import httpx2
import pytest

from standard_annotation_backend.entity_sources.http import (
    EntitySourceClient,
    EntitySourceError,
)
from standard_annotation_backend.entity_sources.registry import ConfiguredEntitySource

SOURCE = ConfiguredEntitySource(
    key="mgi", format="gpi", url="https://example.org/mgi.gpi.gz"
)


def _client(handler: httpx2.MockTransport) -> EntitySourceClient:
    return EntitySourceClient(
        connect_timeout_seconds=1, read_timeout_seconds=1, transport=handler
    )


def test_plain_text_is_decoded_with_checksum_of_downloaded_bytes() -> None:
    """Uncompressed UTF-8 bodies are returned with their SHA-256."""
    body = b"!gpi-version: 2.0\n"
    client = _client(httpx2.MockTransport(lambda _: httpx2.Response(200, content=body)))

    document = client.fetch(SOURCE)

    assert document.source_key == "mgi"
    assert document.source_url == SOURCE.url
    assert document.source_checksum == sha256(body).hexdigest()
    assert document.text == "!gpi-version: 2.0\n"
    assert document.fetched_at.tzinfo is not None


def test_gzip_body_is_expanded_and_checksum_covers_compressed_bytes() -> None:
    """The checksum identifies the downloaded file, not the expanded text."""
    body = gzip.compress("é\n".encode())
    client = _client(httpx2.MockTransport(lambda _: httpx2.Response(200, content=body)))

    document = client.fetch(SOURCE)

    assert document.text == "é\n"
    assert document.source_checksum == sha256(body).hexdigest()


def test_redirects_are_followed_and_configured_url_is_recorded() -> None:
    """Redirects are followed, and the document records the configured URL."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/mgi.gpi.gz":
            return httpx2.Response(
                302, headers={"Location": "https://cdn.example.org/final.gpi"}
            )
        return httpx2.Response(200, content=b"ok\n")

    document = _client(httpx2.MockTransport(handler)).fetch(SOURCE)

    assert document.text == "ok\n"
    assert document.source_url == SOURCE.url


def test_redirect_loop_fails_with_source_error() -> None:
    """A redirect loop raises `EntitySourceError` with the code `source_error`."""
    client = _client(
        httpx2.MockTransport(
            lambda request: httpx2.Response(302, headers={"Location": str(request.url)})
        )
    )
    with pytest.raises(EntitySourceError) as caught:
        client.fetch(SOURCE)
    assert caught.value.code == "source_error"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx2.Response(404), "http_status"),
        (httpx2.Response(503), "http_status"),
        (httpx2.Response(200, content=b"\x1f\x8bnot-gzip"), "invalid_gzip"),
        (httpx2.Response(200, content=b"\xff\xfe"), "invalid_utf8"),
    ],
)
def test_response_failures_have_stable_codes(
    response: httpx2.Response, code: str
) -> None:
    """Error statuses and invalid gzip or UTF-8 bodies raise `EntitySourceError`."""
    client = _client(httpx2.MockTransport(lambda _: response))
    with pytest.raises(EntitySourceError) as caught:
        client.fetch(SOURCE)
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx2.ConnectTimeout("slow"), "timeout"),
        (httpx2.ReadTimeout("slow"), "timeout"),
        (httpx2.ConnectError("refused"), "source_error"),
    ],
)
def test_transport_failures_have_stable_codes(error: Exception, code: str) -> None:
    """Timeouts and connection failures raise `EntitySourceError` with a code.

    The error message omits the URL and does not chain the original exception.
    """

    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise error

    with pytest.raises(EntitySourceError) as caught:
        _client(httpx2.MockTransport(handler)).fetch(SOURCE)
    assert caught.value.code == code
    assert SOURCE.url not in str(caught.value)
    assert caught.value.__cause__ is None
