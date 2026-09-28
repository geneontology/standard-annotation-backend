"""Retrieve authorization data from one immutable GitHub revision."""

from dataclasses import dataclass

import httpx2

from standard_annotation_backend.integrations.github import (
    GitHubClient,
    GitHubIntegrationError,
)


class AuthorizationSourceUnavailableError(Exception):
    """Report that the authorization source could not be read."""


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
        try:
            with httpx2.Client() as client:
                github = GitHubClient(client=client)
                commit_sha = github.resolve_commit(self.repository, self.ref)
                content = github.fetch_repository_content(
                    self.repository,
                    self.path,
                    commit_sha,
                )
            yaml_text = content.decode("utf-8")
        except (GitHubIntegrationError, UnicodeDecodeError):
            raise AuthorizationSourceUnavailableError(
                "go-site users.yaml is unavailable"
            ) from None

        return AuthorizationSourceDocument(
            yaml_text=yaml_text,
            source_repository=self.repository,
            source_commit_sha=commit_sha,
        )

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
        try:
            if source_commit_sha != source_commit_sha.lower():
                raise GitHubIntegrationError
            with httpx2.Client() as client:
                content = GitHubClient(client=client).fetch_repository_content(
                    self.repository,
                    self.path,
                    source_commit_sha,
                )
            yaml_text = content.decode("utf-8")
        except (GitHubIntegrationError, UnicodeDecodeError):
            raise AuthorizationSourceUnavailableError(
                "go-site users.yaml is unavailable"
            ) from None

        return AuthorizationSourceDocument(
            yaml_text=yaml_text,
            source_repository=self.repository,
            source_commit_sha=source_commit_sha,
        )
