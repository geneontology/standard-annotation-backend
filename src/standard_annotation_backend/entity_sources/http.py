"""Retrieve configured entity source files over HTTPS.

Source URLs come only from the reviewed registry file, so retrieval uses an
ordinary HTTP client with timeouts and normal redirect handling. It does not
limit response sizes.
"""

from __future__ import annotations

import gzip
import hashlib
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx2

from standard_annotation_backend.entity_sources.registry import ConfiguredEntitySource

_GZIP_SIGNATURE = b"\x1f\x8b"
_FAILURE_CODES = frozenset(
    {"source_error", "http_status", "timeout", "invalid_gzip", "invalid_utf8"}
)


@dataclass(frozen=True, slots=True)
class EntitySourceDocument:
    """Contain retrieved source text and the details that identify it.

    Attributes:
        source_key: Registry key of the retrieved source.
        source_url: The configured URL, even when redirects were followed.
        source_checksum: Lowercase SHA-256 of the downloaded bytes, before any
            gzip expansion.
        fetched_at: When retrieval completed.
        text: Decoded UTF-8 source text.
    """

    source_key: str
    source_url: str
    source_checksum: str
    fetched_at: datetime
    text: str


class EntitySourceError(RuntimeError):
    """Report a retrieval failure without including URLs or source content."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _FAILURE_CODES else "source_error"
        super().__init__(f"Entity source retrieval failed: {self.code}")


class EntitySourceClient:
    """Retrieve and decode configured entity source files."""

    def __init__(
        self,
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._timeout = httpx2.Timeout(
            read_timeout_seconds, connect=connect_timeout_seconds
        )
        self._transport = transport

    def fetch(self, source: ConfiguredEntitySource) -> EntitySourceDocument:
        """Retrieve one source, expanding gzip and decoding strict UTF-8.

        Raises:
            EntitySourceError: If retrieval, gzip expansion, or decoding fails.
        """
        try:
            with httpx2.Client(
                follow_redirects=True, timeout=self._timeout, transport=self._transport
            ) as client:
                response = client.get(source.url)
                response.raise_for_status()
                body = response.content
        except httpx2.TimeoutException:
            raise EntitySourceError("timeout") from None
        except httpx2.HTTPStatusError:
            raise EntitySourceError("http_status") from None
        except httpx2.HTTPError:
            raise EntitySourceError("source_error") from None
        checksum = hashlib.sha256(body).hexdigest()
        if body.startswith(_GZIP_SIGNATURE):
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError, zlib.error):
                raise EntitySourceError("invalid_gzip") from None
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise EntitySourceError("invalid_utf8") from None
        return EntitySourceDocument(
            source_key=source.key,
            source_url=source.url,
            source_checksum=checksum,
            fetched_at=datetime.now(UTC),
            text=text,
        )
