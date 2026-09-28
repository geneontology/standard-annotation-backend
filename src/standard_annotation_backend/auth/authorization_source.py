"""Retrieve authorization data from one immutable GitHub revision."""

from dataclasses import dataclass
from typing import Annotated

import httpx2
from pydantic import BaseModel, StringConstraints, ValidationError

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 30


class AuthorizationSourceUnavailableError(Exception):
    """Report that the authorization source could not be read."""


class _GitCommit(BaseModel):
    """Validate the immutable commit returned by GitHub."""

    sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


@dataclass(frozen=True, slots=True)
class AuthorizationSourceDocument:
    """Store authorization YAML with the repository and commit that supplied it."""

    yaml_text: str
    source_repository: str
    source_commit_sha: str


@dataclass(frozen=True, slots=True)
class AuthorizationSourceClient:
    """Retrieve a configured users document from an exact GitHub commit."""

    repository: str
    ref: str
    path: str

    def fetch(self) -> AuthorizationSourceDocument:
        """Resolve the configured ref and retrieve the document at that commit.

        Returns:
            The authorization YAML, repository, and exact lowercase commit SHA.

        Raises:
            AuthorizationSourceUnavailableError: If a GitHub request fails, returns
                an invalid response, redirects, or returns non-UTF-8 content.
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            commit_response = httpx2.get(
                f"{GITHUB_API}/repos/{self.repository}/commits/{self.ref}",
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
            if commit_response.status_code != 200:
                raise AuthorizationSourceUnavailableError
            commit = _GitCommit.model_validate(commit_response.json())

        except (
            httpx2.RequestError,
            ValueError,
            ValidationError,
            AuthorizationSourceUnavailableError,
        ):
            raise AuthorizationSourceUnavailableError(
                "go-site users.yaml is unavailable"
            ) from None

        return self.fetch_at_commit(commit.sha)

    def fetch_at_commit(self, source_commit_sha: str) -> AuthorizationSourceDocument:
        """Retrieve the source at an exact lowercase Git commit SHA.

        Args:
            source_commit_sha: Full commit SHA previously resolved for this source.

        Returns:
            The authorization YAML, repository, and supplied commit SHA.

        Raises:
            AuthorizationSourceUnavailableError: If the SHA is invalid, the request
                fails or redirects, or GitHub does not return a UTF-8 document.
        """
        headers = {
            "Accept": "application/vnd.github.raw+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            commit = _GitCommit(sha=source_commit_sha)
            source_response = httpx2.get(
                f"{GITHUB_API}/repos/{self.repository}/contents/{self.path}",
                params={"ref": commit.sha},
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
            if source_response.status_code != 200:
                raise AuthorizationSourceUnavailableError
            yaml_text = source_response.content.decode("utf-8")
        except (
            httpx2.RequestError,
            UnicodeDecodeError,
            ValueError,
            ValidationError,
            AuthorizationSourceUnavailableError,
        ):
            raise AuthorizationSourceUnavailableError(
                "go-site users.yaml is unavailable"
            ) from None

        return AuthorizationSourceDocument(
            yaml_text=yaml_text,
            source_repository=self.repository,
            source_commit_sha=source_commit_sha,
        )
