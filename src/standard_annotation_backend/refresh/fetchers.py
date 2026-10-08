"""Fetch configured reference data sources.

There is one fetcher per source type. Both return a `SourceDocument`: the
downloaded bytes plus provenance that identifies exactly what was fetched. Each
refresh service decodes the bytes itself. Failures raise `SourceError` with a fixed
code; messages and logs never include URLs or content.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Protocol

import httpx2

from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    SourceDocument,
    TerminalRefreshError,
)
from standard_annotation_backend.integrations.github import (
    GitHubClient,
    GitHubIntegrationError,
)
from standard_annotation_backend.refresh.sources import GitHubSource, HttpsSource

_RETRIEVAL_CODES = frozenset(
    {
        RefreshFailureCode.SOURCE_ERROR,
        RefreshFailureCode.HTTP_STATUS,
        RefreshFailureCode.TIMEOUT,
        RefreshFailureCode.INVALID_GZIP,
        RefreshFailureCode.INVALID_UTF8,
    }
)


class SourceError(TerminalRefreshError):
    """Report a retrieval or decoding failure with a fixed code.

    Attributes:
        code: One of `source_error`, `http_status`, `timeout`, `invalid_gzip`, or
            `invalid_utf8`.

    Raises:
        ValueError: If `code` is not one of those retrieval codes.
    """

    def __init__(self, code: RefreshFailureCode) -> None:
        if code not in _RETRIEVAL_CODES:
            raise ValueError("source errors require a retrieval failure code")
        self.code = code
        super().__init__(
            f"Source retrieval failed: {self.code.value}", failure_code=self.code
        )


class SourceFetcher(Protocol):
    """Fetch one configured source.

    `SourceFetchers` is the production implementation. Tests substitute a fake
    that returns fixed documents without using the network.
    """

    def fetch(
        self, source_key: str, source: GitHubSource | HttpsSource
    ) -> SourceDocument:
        """Return the fetched document.

        Raises:
            SourceError: If retrieval fails.
        """
        ...


class GitHubSourceFetcher:
    """Fetch a GitHub file at the commit its configured ref currently names."""

    def __init__(self, client: httpx2.Client, timeout: httpx2.Timeout) -> None:
        self._github = GitHubClient(client=client, timeout=timeout)

    def fetch(self, source_key: str, source: GitHubSource) -> SourceDocument:
        """Resolve the ref, then download the path at the resolved commit.

        Raises:
            SourceError: With `source_error` if GitHub cannot supply either.
        """
        try:
            commit = self._github.resolve_commit(source.repository, source.ref)
            content = self._github.fetch_raw_content(
                source.repository, source.path, commit
            )
        except GitHubIntegrationError:
            raise SourceError(RefreshFailureCode.SOURCE_ERROR) from None
        return SourceDocument(
            source_key=source_key,
            source_type="github",
            source_locator=f"github:{source.repository}:{source.path}",
            source_revision=commit,
            source_checksum=hashlib.sha256(content).hexdigest(),
            fetched_at=datetime.now(UTC),
            content=content,
        )


class HttpsSourceFetcher:
    """Fetch a configured HTTPS URL, following redirects."""

    def __init__(self, client: httpx2.Client, timeout: httpx2.Timeout) -> None:
        self._client = client
        self._timeout = timeout

    def fetch(self, source_key: str, source: HttpsSource) -> SourceDocument:
        """Download the URL.

        Raises:
            SourceError: With `timeout`, `http_status`, or `source_error`
                (including when a redirect leaves HTTPS).
        """
        url = str(source.url)
        try:
            response = self._client.get(
                url, follow_redirects=True, timeout=self._timeout
            )
            response.raise_for_status()
            content = response.content
            # The configured URL is HTTPS, but a redirect could send the request
            # to plain `http`, where anyone on the network path could replace
            # the document. Some kinds (authorization) grant privileges from
            # what they read, so a downgrade at any hop fails the fetch.
            if response.url.scheme != "https" or any(
                hop.url.scheme != "https" for hop in response.history
            ):
                raise SourceError(RefreshFailureCode.SOURCE_ERROR)
        except httpx2.TimeoutException:
            raise SourceError(RefreshFailureCode.TIMEOUT) from None
        except httpx2.HTTPStatusError:
            raise SourceError(RefreshFailureCode.HTTP_STATUS) from None
        except httpx2.HTTPError:
            raise SourceError(RefreshFailureCode.SOURCE_ERROR) from None
        return SourceDocument(
            source_key=source_key,
            source_type="https",
            source_locator=url,
            source_revision=None,
            source_checksum=hashlib.sha256(content).hexdigest(),
            fetched_at=datetime.now(UTC),
            content=content,
        )


class SourceFetchers:
    """Fetch any configured source with the fetcher for its type.

    One instance shares one HTTP client and one timeout configuration across
    every fetch in a worker operation.
    """

    def __init__(
        self,
        client: httpx2.Client,
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
    ) -> None:
        timeout = httpx2.Timeout(read_timeout_seconds, connect=connect_timeout_seconds)
        self._github = GitHubSourceFetcher(client, timeout)
        self._https = HttpsSourceFetcher(client, timeout)

    def fetch(
        self, source_key: str, source: GitHubSource | HttpsSource
    ) -> SourceDocument:
        """Fetch one source.

        Raises:
            SourceError: If retrieval fails.
        """
        if isinstance(source, GitHubSource):
            return self._github.fetch(source_key, source)
        return self._https.fetch(source_key, source)
