"""Map supported ontology keys to their definitions and configured sources."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import httpx2

from standard_annotation_backend.config import (
    GitHubOntologySourceSettings,
    OntologySourceSettings,
)
from standard_annotation_backend.domain.ontology import OntologyDefinition, OntologyKey
from standard_annotation_backend.ontology.sources import (
    GitHubOntologySource,
    OntologySource,
)

GO_DEFINITION = OntologyDefinition(
    key=OntologyKey.GO,
    identifier_prefixes=("GO",),
    closure_predicates=("rdfs:subClassOf", "BFO:0000050"),
    document_ontology_id="go",
)

_DEFINITIONS = {OntologyKey.GO: GO_DEFINITION}


class OntologyRegistry:
    """Provide definitions and source adapters for configured ontologies."""

    def __init__(
        self,
        sources: Mapping[OntologyKey, OntologySourceSettings],
        *,
        client_factory: Callable[[], httpx2.Client] = httpx2.Client,
    ) -> None:
        self._sources = dict(sources)
        self._client_factory = client_factory

    @property
    def keys(self) -> tuple[OntologyKey, ...]:
        """Return configured ontology keys in stable order."""
        return tuple(sorted(self._sources))

    def definition(self, key: OntologyKey) -> OntologyDefinition:
        """Return valid identifier prefixes and closure predicates for an ontology."""
        return _DEFINITIONS[key]

    def source(self, key: OntologyKey) -> OntologySource:
        """Create the source adapter configured for one ontology."""
        settings = self._sources[key]
        if isinstance(settings, GitHubOntologySourceSettings):
            return GitHubOntologySource(
                ontology_key=key,
                repository=settings.repository,
                ref=settings.ref,
                path=settings.path,
                client=self._client_factory(),
            )
        raise AssertionError("validated ontology source has no adapter")
