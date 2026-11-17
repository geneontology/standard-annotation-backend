"""Provide source fakes and runner construction for refresh integration tests."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from seeding import create_job
from sqlalchemy import Engine

from standard_annotation_backend.domain.jobs import JobType, ProgressReporter
from standard_annotation_backend.domain.refresh import (
    RefreshOutcome,
    SourceDocument,
    SourceProvenance,
    pack_source_files,
)
from standard_annotation_backend.go_site.rule_files import RULE_FILE_PATHS
from standard_annotation_backend.persistence.locks import bind_try_lock
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh.runner import RefreshKind
from standard_annotation_backend.refresh.sources import (
    ConfiguredSource,
    GitHubRulesSource,
    GitHubSource,
    RefreshSources,
    SourcesFile,
)
from standard_annotation_backend.services.annotation_scan import AnnotationScanRunner
from standard_annotation_backend.services.authorization_refresh_service import (
    AuthorizationRefreshService,
)
from standard_annotation_backend.services.job_executors import JobExecutors
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.qc_report_service import QcReportScan
from standard_annotation_backend.services.term_replacement_service import (
    TermReplacementScan,
)
from standard_annotation_backend.workers.runtime import create_job_executors

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


GO_ENTRY: dict[str, object] = {
    "type": "github",
    "repository": "geneontology/go-ontology",
    "ref": "master",
    "path": "src/ontology/go-edit.obo",
    "document_ontology_id": "go",
    "prefixes": ["GO"],
    "closure_predicates": ["rdfs:subClassOf", "BFO:0000050"],
    "required_subsets": ["gocheck_do_not_annotate", "gocheck_obsoletion_candidate"],
}
RULES_ENTRY: dict[str, object] = {
    "type": "github",
    "repository": "geneontology/go-site",
    "ref": "master",
}
RULES_LOCATOR = "github:geneontology/go-site"
GO_SITE_FIXTURES = Path(__file__).parents[1] / "fixtures" / "go_site"


def rule_files(replacements: Mapping[str, bytes] | None = None) -> bytes:
    """Return the go-site fixture files packed as the rules fetcher packs them.

    Args:
        replacements: Contents to use instead of fixtures, keyed by go-site path,
            such as `metadata/gorefs.yaml`.
    """
    files = {
        path: (GO_SITE_FIXTURES / PurePosixPath(path).name).read_bytes()
        for path in RULE_FILE_PATHS
    }
    return pack_source_files({**files, **(replacements or {})})


def sources_with(
    *,
    entities: dict[str, object] | None = None,
    annotations: dict[str, object] | None = None,
    ontologies: dict[str, object] | None = None,
) -> RefreshSources:
    """Return test sources with the standard authorization, rules, and GO entries.

    Args:
        entities: The `entities` section of the sources file; empty if omitted.
        annotations: The `annotations` section of the sources file; empty if
            omitted.
        ontologies: Ontology entries to configure in addition to GO.
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
                "rules": RULES_ENTRY,
                "ontologies": {"go": GO_ENTRY, **(ontologies or {})},
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
TEST_ONTOLOGIES = TEST_SOURCES.ontology_catalog()
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

    def fetch(self, source_key: str, source: ConfiguredSource) -> SourceDocument:
        """Return the configured document for `source_key` or raise its error."""
        self.calls.append(source_key)
        content = self.contents[source_key]
        if isinstance(content, Exception):
            raise content
        if isinstance(source, GitHubRulesSource):
            return SourceDocument(
                source_key=source_key,
                source_type="github",
                source_locator=f"github:{source.repository}",
                source_revision=REVISION,
                source_checksum=sha256(content).hexdigest(),
                fetched_at=FETCHED_AT,
                content=content,
            )
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
    dispatch_follow_up: Callable[[Job], None] | None = None,
) -> JobExecutors:
    """Build the production runners and registry around fake fetchers."""
    return create_job_executors(
        engine=engine,
        unit_of_work_factory=unit_of_work_factory,
        sources=sources,
        fetchers=fetchers,
        dispatch_follow_up=dispatch_follow_up,
    )


def build_scan_runner(
    engine: Engine, unit_of_work_factory: UnitOfWorkFactory
) -> AnnotationScanRunner:
    """Build the production annotation scan runner and scans for the test ontologies."""
    return AnnotationScanRunner(
        try_lock=bind_try_lock(engine),
        jobs=JobService(unit_of_work_factory),
        unit_of_work_factory=unit_of_work_factory,
        scans=(
            TermReplacementScan(unit_of_work_factory, TEST_ONTOLOGIES),
            QcReportScan(unit_of_work_factory, TEST_ONTOLOGIES),
        ),
    )


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
        'subsetdef: gocheck_do_not_annotate "Term not to be used for direct annotation"',
        'subsetdef: gocheck_obsoletion_candidate "Terms planned for obsoletion"',
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
    created = create_job(
        factory,
        job_type=job_type,
        requested_by=requested_by,
        parameters={"source_key": source_key},
    )
    return JobService(factory).start(created.job_id)


def source_gpi(*identifiers: str) -> str:
    """Return valid GPI text with one line per identifier, in the given order."""
    text = "!gpi-version: 2.0\n!generated-by: TestDB\n!date-generated: 2026-09-29\n"
    return text + "".join(
        f"{identifier}\tSYM{index}\tProtein\t\tSO:0001217\tNCBITaxon:9606\t\t{identifier}\t\t\t\n"
        for index, identifier in enumerate(identifiers)
    )


def gpi_bytes(*identifiers: str) -> bytes:
    """Return a valid GPI 2.0 file that lists `identifiers`."""
    return source_gpi(*identifiers).encode()


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
    return apply_in_transaction(
        factory,
        AuthorizationRefreshService(),
        job,
        users_document(yaml_text, provenance),
    )


def ignore_progress(
    phase: str,
    counts: Mapping[str, int] | None = None,
    warnings: tuple[str, ...] = (),
) -> None:
    """Accept progress reports from `apply` without storing them."""


def apply_in_transaction(
    factory: UnitOfWorkFactory,
    kind: RefreshKind,
    job: Job,
    document: SourceDocument,
    report: ProgressReporter = ignore_progress,
) -> RefreshOutcome:
    """Run a kind's `apply` and record the job's success in one transaction.

    As the runner does, the job's result and final progress commit together with
    what `apply` stored, so a job that `apply` finishes never stays running. If
    `apply` raises, nothing commits and the job stays running. Jobs that `apply`
    queued are not sent.
    """
    with factory() as unit_of_work:
        outcome = kind.apply(unit_of_work, job, document, report)
        JobService(factory).record_success(
            unit_of_work,
            job.job_id,
            result={**outcome.result, "unchanged": outcome.unchanged},
            progress={
                **outcome.counts,
                "phase": "completed",
                "unchanged": outcome.unchanged,
            },
            warnings=outcome.warnings,
        )
        unit_of_work.commit()
    return outcome
