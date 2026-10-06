"""Provide source fakes and runner construction for refresh integration tests."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, Protocol, cast

import pytest
from sqlalchemy import Engine

from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import (
    RefreshOutcome,
    SourceDocument,
    SourceProvenance,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh.runner import RefreshRunner
from standard_annotation_backend.refresh.sources import (
    GitHubSource,
    HttpsSource,
    RefreshSources,
    SourcesFile,
)
from standard_annotation_backend.services.authorization_refresh_service import (
    AuthorizationRefreshService,
)
from standard_annotation_backend.services.job_service import Job, JobService
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
FETCHED_AT = datetime(2026, 10, 1, tzinfo=UTC)


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
                fetched_at=FETCHED_AT,
                content=content,
            )
        return SourceDocument(
            source_key=source_key,
            source_type="https",
            source_locator=str(source.url),
            source_revision=None,
            source_checksum=sha256(content).hexdigest(),
            fetched_at=FETCHED_AT,
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


def source_document(
    source_key: str,
    content: bytes,
    *,
    source_type: Literal["github", "https"] = "https",
    source_locator: str | None = None,
    source_revision: str | None = None,
    fetched_at: datetime = FETCHED_AT,
) -> SourceDocument:
    """Build a fetched document whose checksum matches `content`."""
    return SourceDocument(
        source_key=source_key,
        source_type=source_type,
        source_locator=source_locator or f"https://example.org/{source_key}",
        source_revision=source_revision,
        source_checksum=sha256(content).hexdigest(),
        fetched_at=fetched_at,
        content=content,
    )


@dataclass(frozen=True, slots=True)
class OboTerm:
    """Describe one term written by `obo`."""

    term_id: str
    obsolete: bool = False
    replaced_by: tuple[str, ...] = ()
    consider: tuple[str, ...] = ()
    is_a: tuple[str, ...] = ()


def obo(*terms: OboTerm, data_version: str = "releases/2026-09-28") -> bytes:
    """Write a minimal GO OBO document containing `terms`."""
    lines = [
        "format-version: 1.4",
        f"data-version: {data_version}",
        "ontology: go",
        "",
    ]
    for term in terms:
        lines += ["[Term]", f"id: {term.term_id}", f"name: {term.term_id}"]
        lines += [f"is_a: {parent}" for parent in term.is_a]
        if term.obsolete:
            lines.append("is_obsolete: true")
        lines += [f"replaced_by: {target}" for target in term.replaced_by]
        lines += [f"consider: {target}" for target in term.consider]
        lines.append("")
    return "\n".join(lines).encode()


GO_LOCATOR = "github:geneontology/go-ontology:src/ontology/go-edit.obo"


def go_document(content: bytes) -> SourceDocument:
    """Build the GO document the configured test source would fetch."""
    return source_document(
        "go",
        content,
        source_type="github",
        source_revision=REVISION,
        source_locator=GO_LOCATOR,
    )


def start_job(
    factory: UnitOfWorkFactory,
    job_type: JobType,
    source_key: str,
    *,
    requested_by: str = "curator",
) -> Job:
    """Create and start a refresh job, as the runner would before `apply`."""
    jobs = JobService(factory)
    created = jobs.create(
        job_type=job_type,
        requested_by=requested_by,
        parameters={"source_key": source_key},
    )
    return jobs.start(created.job_id)


def users_document(yaml_text: str, provenance: SourceProvenance) -> SourceDocument:
    """Build a `users.yaml` document described by `provenance`."""
    return SourceDocument(
        source_key="go-site",
        source_type=cast(Literal["github", "https"], provenance.source_type),
        source_locator=provenance.source_locator,
        source_revision=provenance.source_revision,
        source_checksum=provenance.source_checksum,
        fetched_at=provenance.fetched_at,
        content=yaml_text.encode(),
    )


def apply_users_yaml(
    factory: UnitOfWorkFactory,
    yaml_text: str,
    provenance: SourceProvenance,
    *,
    requested_by: str = "authorization-refresh",
) -> RefreshOutcome:
    """Apply a users document through a started authorization job."""
    job = start_job(
        factory, JobType.AUTHORIZATION_REFRESH, "go-site", requested_by=requested_by
    )
    return AuthorizationRefreshService(factory).apply(
        job, users_document(yaml_text, provenance), ignore_progress
    )


def ignore_progress(
    phase: str,
    counts: Mapping[str, int] | None = None,
    warnings: tuple[str, ...] = (),
) -> None:
    """Accept progress reports from `apply` without storing them."""


class RefreshKindLike(Protocol):
    """Describe the `apply` method that `stage_without_publishing` calls."""

    def apply(
        self,
        job: Job,
        document: SourceDocument,
        report: Callable[..., None],
    ) -> RefreshOutcome:
        """Apply `document` for `job`, reporting progress through `report`."""
        ...


class _StopAfterStaging(Exception):
    """Interrupt `apply` after its staging transaction has committed."""


def stage_without_publishing(
    service: RefreshKindLike,
    job: Job,
    document: SourceDocument,
    publication: tuple[type, str],
) -> None:
    """Run `apply` but stop at publication, leaving committed staging.

    This reproduces a worker that stopped between staging and publication.

    Args:
        service: The refresh service under test.
        job: A started job.
        document: The document to stage.
        publication: The repository class and method name that begins
            publication, for example `(EntityRepository, "publish")`.
    """

    def stop(*_args: object, **_kwargs: object) -> None:
        raise _StopAfterStaging

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(*publication, stop)
        with pytest.raises(_StopAfterStaging):
            service.apply(job, document, ignore_progress)
