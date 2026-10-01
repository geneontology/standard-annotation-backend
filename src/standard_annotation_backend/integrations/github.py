"""Provide GitHub requests shared by SAB feature adapters.

This module defines GitHub URLs, headers, timeouts, commit validation, and
request errors. Feature adapters call it and translate `GitHubIntegrationError`
into errors for their callers. This module must not import those adapters or
SAB's domain, service, persistence, or API layers.
"""

from dataclasses import dataclass
from typing import Annotated

import httpx2
from pydantic import BaseModel, StringConstraints, ValidationError, field_validator

_API_URL = "https://api.github.com"
_RAW_URL = "https://raw.githubusercontent.com"
_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT_SECONDS = 30
_SAFE_ERROR = "GitHub resource is unavailable"


class GitHubIntegrationError(RuntimeError):
    """Report that a GitHub request or response could not be used."""


class _GitCommit(BaseModel):
    """Validate and normalize an immutable Git commit identifier."""

    sha: Annotated[str, StringConstraints(pattern=r"^[0-9A-Fa-f]{40}$")]

    @field_validator("sha")
    @classmethod
    def normalize_sha(cls, value: str) -> str:
        """Return a commit identifier in lowercase."""
        return value.lower()


@dataclass(frozen=True, slots=True)
class GitHubClient:
    """Resolve commits and retrieve repository content from GitHub.

    Attributes:
        client: HTTP client that sends the requests.
        timeout: Timeout applied to each request. Defaults to 30 seconds.
    """

    client: httpx2.Client
    timeout: httpx2.Timeout | float = _REQUEST_TIMEOUT_SECONDS

    def resolve_commit(self, repository: str, ref: str) -> str:
        """Resolve a repository ref to a canonical immutable commit SHA.

        Args:
            repository: GitHub repository in `owner/name` form.
            ref: Branch, tag, or commit understood by GitHub.

        Returns:
            The full lowercase commit SHA.

        Raises:
            GitHubIntegrationError: If GitHub cannot provide a valid commit.
        """
        try:
            response = self.client.get(
                f"{_API_URL}/repos/{repository}/commits/{ref}",
                headers=self._api_headers("application/vnd.github+json"),
                timeout=self.timeout,
                follow_redirects=False,
            )
            response.raise_for_status()
            return _GitCommit.model_validate(response.json()).sha
        except (httpx2.HTTPError, ValueError, ValidationError):
            raise GitHubIntegrationError(_SAFE_ERROR) from None

    def fetch_raw_content(self, repository: str, path: str, commit_sha: str) -> bytes:
        """Retrieve raw repository bytes at an immutable commit.

        Args:
            repository: GitHub repository in `owner/name` form.
            path: Repository-relative path to retrieve.
            commit_sha: Full hexadecimal commit identifier.

        Returns:
            The raw response body at the normalized immutable commit.

        Raises:
            GitHubIntegrationError: If the commit is invalid or retrieval fails.
        """
        commit = self._validate_commit(commit_sha)
        try:
            response = self.client.get(
                f"{_RAW_URL}/{repository}/{commit.sha}/{path}",
                timeout=self.timeout,
                follow_redirects=False,
            )
            response.raise_for_status()
            return response.content
        except httpx2.HTTPError:
            raise GitHubIntegrationError(_SAFE_ERROR) from None

    @staticmethod
    def _api_headers(accept: str) -> dict[str, str]:
        """Build headers shared by versioned GitHub API requests."""
        return {
            "Accept": accept,
            "X-GitHub-Api-Version": _API_VERSION,
        }

    @staticmethod
    def _validate_commit(commit_sha: str) -> _GitCommit:
        """Return a validated commit or raise the shared GitHub error."""
        try:
            return _GitCommit(sha=commit_sha)
        except ValidationError:
            raise GitHubIntegrationError(_SAFE_ERROR) from None
