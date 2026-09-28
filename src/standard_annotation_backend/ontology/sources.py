"""Retrieve ontology documents from configured external sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

import httpx2

from standard_annotation_backend.domain.ontology import OntologyDocument, OntologyKey
from standard_annotation_backend.integrations.github import (
    GitHubClient,
    GitHubIntegrationError,
)

_SAFE_ERROR = "Configured ontology source is unavailable"


class OntologySource(Protocol):
    """Define how callers retrieve one configured ontology document."""

    def fetch(self) -> OntologyDocument:
        """Return retrieved bytes and the source details that identify them."""
        raise NotImplementedError


class OntologySourceError(RuntimeError):
    """Report that a configured ontology document could not be retrieved."""


@dataclass(frozen=True, slots=True)
class GitHubOntologySource:
    """Resolve a GitHub ref and retrieve OBO bytes at its exact commit."""

    ontology_key: OntologyKey
    repository: str
    ref: str
    path: str
    client: httpx2.Client

    def fetch(self) -> OntologyDocument:
        """Retrieve configured bytes after resolving the mutable ref.

        Returns:
            Source bytes with an immutable Git commit and checksum.

        Raises:
            OntologySourceError: If either request or its response is unusable.
        """
        try:
            github = GitHubClient(client=self.client)
            commit_sha = github.resolve_commit(self.repository, self.ref)
            content = github.fetch_raw_content(
                self.repository,
                self.path,
                commit_sha,
            )
            if not content:
                raise OntologySourceError
        except (GitHubIntegrationError, OntologySourceError):
            raise OntologySourceError(_SAFE_ERROR) from None

        return OntologyDocument(
            ontology_key=self.ontology_key,
            content=content,
            source_type="github",
            source_locator=f"github:{self.repository}:{self.path}",
            source_revision=commit_sha,
            source_checksum=sha256(content).hexdigest(),
            retrieved_at=datetime.now(UTC),
        )
