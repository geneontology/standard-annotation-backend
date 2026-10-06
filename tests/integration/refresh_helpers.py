"""Provide source fakes and runner construction for refresh integration tests."""

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import Engine

from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh.fetchers import SourceDocument
from standard_annotation_backend.refresh.runner import RefreshRunner
from standard_annotation_backend.refresh.sources import (
    GitHubSource,
    HttpsSource,
    RefreshSources,
    SourcesFile,
)
from standard_annotation_backend.workers.runtime import create_refresh_runner

TEST_ANNOTATION_SOURCES: dict[str, object] = {
    "mgi-gpad": {
        "type": "https",
        "group": "MGI",
        "url": "https://example.org/mgi.gpad",
    },
    "rgd-gpad": {
        "type": "https",
        "group": "RGD",
        "url": "https://example.org/rgd.gpad",
    },
}


def sources_with(
    *,
    entities: dict[str, object] | None = None,
    annotations: dict[str, object] | None = None,
) -> RefreshSources:
    """Return test sources with the standard authorization and GO entries.

    Args:
        entities: The `entities` section of the sources file; empty if omitted.
        annotations: The `annotations` section of the sources file; empty if
            omitted.
    """
    return RefreshSources(
        SourcesFile.model_validate(
            {
                "authorization": {
                    "type": "github",
                    "repository": "geneontology/go-site",
                    "ref": "master",
                    "path": "metadata/users.yaml",
                },
                "ontologies": {
                    "go": {
                        "type": "github",
                        "repository": "geneontology/go-ontology",
                        "ref": "master",
                        "path": "src/ontology/go-edit.obo",
                    }
                },
                "entities": entities or {},
                "annotations": annotations or {},
            }
        )
    )


def sources_with_entities(entities: dict[str, object]) -> RefreshSources:
    """Return test sources with the given entity sources and the test GPAD sources."""
    return sources_with(entities=entities, annotations=TEST_ANNOTATION_SOURCES)


TEST_SOURCES = sources_with_entities(
    {
        "mgi": {"type": "https", "url": "https://example.org/mgi.gpi"},
        "rgd": {"type": "https", "url": "https://example.org/rgd.gpi"},
    }
)
REVISION = "a" * 40


class FakeFetchers:
    """Return fixed documents by source key instead of using the network.

    Attributes:
        contents: Bytes to return, or an exception to raise, for each source key.
        calls: Source keys fetched, in order.
    """

    def __init__(self, contents: dict[str, bytes | Exception]) -> None:
        self.contents = contents
        self.calls: list[str] = []

    def fetch(
        self, source_key: str, source: GitHubSource | HttpsSource
    ) -> SourceDocument:
        """Return the configured document for `source_key` or raise its error."""
        self.calls.append(source_key)
        content = self.contents[source_key]
        if isinstance(content, Exception):
            raise content
        if isinstance(source, GitHubSource):
            return SourceDocument(
                source_key=source_key,
                source_type="github",
                source_locator=f"github:{source.repository}:{source.path}",
                source_revision=REVISION,
                source_checksum=sha256(content).hexdigest(),
                fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
                content=content,
            )
        return SourceDocument(
            source_key=source_key,
            source_type="https",
            source_locator=str(source.url),
            source_revision=None,
            source_checksum=sha256(content).hexdigest(),
            fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
            content=content,
        )


def build_runner(
    engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    fetchers: FakeFetchers,
    *,
    sources: RefreshSources = TEST_SOURCES,
    enqueue_prune: Callable[[str], None] | None = None,
) -> RefreshRunner:
    """Build the production runner and kinds around fake fetchers."""
    return create_refresh_runner(
        engine=engine,
        unit_of_work_factory=unit_of_work_factory,
        sources=sources,
        fetchers=fetchers,
        enqueue_prune=enqueue_prune,
    ).runner
