"""Verify that rules refreshes activate the go-site files GO Rules read together."""

from uuid import UUID

from refresh_helpers import (
    REVISION,
    RULES_LOCATOR,
    TEST_ONTOLOGIES,
    TEST_SOURCES,
    FakeFetchers,
    apply_in_transaction,
    build_runner,
    rule_files,
    source_document,
    sources_with,
    start_job,
)
from seeding import create_job
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import SourceDocument
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.go_site.rule_files import (
    EXTENSIONS_CONSTRAINTS_PATH,
    GOREFS_PATH,
    RETRACTED_PUBLICATIONS_PATH,
)
from standard_annotation_backend.persistence.models import (
    ReferenceSnapshotRecord,
    RetractedPublicationRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.rules_refresh_service import (
    RulesRefreshService,
)

FIXTURE_PMIDS = {"PMID:10026173", "PMID:10047653", "PMID:10066663"}
XAO_EXTENSIONS = (
    b"- relation: part_of\n"
    b"  relation_id: BFO:0000050\n"
    b"  namespaces: [ZFA, XAO, MGI]\n"
    b"  primary_root_terms: [GO:0008150]\n"
)
XAO_WARNING = (
    "extensions-constraints.yaml namespace XAO is neither a configured ontology "
    "prefix nor a db-xrefs.yaml database"
)


def _ontology(prefix: str) -> dict[str, object]:
    """Return a term-list ontology entry that owns `prefix`."""
    key = prefix.lower()
    return {
        "type": "https",
        "url": f"https://example.org/{key}.obo",
        "document_ontology_id": key,
        "prefixes": [prefix],
    }


def _run(
    engine: Engine,
    factory: UnitOfWorkFactory,
    content: bytes,
    *,
    sources: RefreshSources = TEST_SOURCES,
) -> Job:
    """Run one rules refresh job over packed `content` and return the finished job."""
    job_id = create_job(
        factory,
        job_type=JobType.RULES_REFRESH,
        requested_by="curator",
        parameters={"source_key": "go-site"},
    ).job_id
    fetchers = FakeFetchers({"go-site": content})
    build_runner(engine, factory, fetchers, sources=sources).run(job_id)
    return JobService(factory).find(job_id)


def _snapshot_id(job: Job) -> UUID:
    """Return the snapshot named in a succeeded job's result."""
    assert job.result is not None
    return UUID(str(job.result["snapshot_id"]))


def _stored(
    session_factory: sessionmaker[Session],
) -> dict[UUID, tuple[bool, set[str]]]:
    """Return whether each stored snapshot is active, and its retracted PMIDs."""
    with session_factory() as session:
        stored: dict[UUID, tuple[bool, set[str]]] = {
            record.snapshot_id: (record.active, set())
            for record in session.scalars(
                select(ReferenceSnapshotRecord).where(
                    ReferenceSnapshotRecord.kind == SnapshotKind.RULES
                )
            )
        }
        rows = session.execute(
            select(
                RetractedPublicationRecord.snapshot_id, RetractedPublicationRecord.pmid
            )
        )
        for snapshot_id, pmid in rows:
            stored[snapshot_id][1].add(pmid)
    return stored


def _document(content: bytes, revision: str) -> SourceDocument:
    """Build the rules document fetched at `revision`."""
    return source_document(
        "go-site",
        content,
        source_type="github",
        source_locator=RULES_LOCATOR,
        source_revision=revision,
    )


def test_refresh_activates_the_six_files_as_one_snapshot(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A refresh stores every file, activates the snapshot, and reports its counts."""
    job = _run(database_engine, unit_of_work_factory, rule_files())

    assert job.status == JobStatus.SUCCEEDED
    assert job.result is not None
    reported = {
        key: value
        for key, value in job.result.items()
        if key not in {"snapshot_id", "source_checksum", "fetched_at", "refreshed_at"}
    }
    assert reported == {
        "database_count": 5,
        "eco_usage_constraint_count": 4,
        "extension_constraint_count": 3,
        "go_ref_count": 3,
        "group_count": 2,
        "retracted_publication_count": 3,
        "source_locator": RULES_LOCATOR,
        "source_revision": REVISION,
        "source_type": "github",
        "unchanged": False,
        "unrecognized_extension_namespaces": [],
    }
    assert job.warnings == ()
    assert _stored(session_factory) == {_snapshot_id(job): (True, FIXTURE_PMIDS)}


def test_an_invalid_file_fails_the_refresh_and_keeps_the_previous_snapshot(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Nothing from a failed refresh is stored, and each issue names its file."""
    first = _run(database_engine, unit_of_work_factory, rule_files())

    failed = _run(
        database_engine,
        unit_of_work_factory,
        rule_files({GOREFS_PATH: b"- id: GO_REF:15\n"}),
    )

    assert failed.status == JobStatus.FAILED
    assert failed.progress["failure_code"] == "invalid_document"
    details = failed.progress["failure_details"]
    assert isinstance(details, dict)
    assert details["issue_count"] == 1
    assert [(issue["location"], issue["type"]) for issue in details["issues"]] == [
        ([GOREFS_PATH, 0, "id"], "string_pattern_mismatch")
    ]
    assert _stored(session_factory) == {_snapshot_id(first): (True, FIXTURE_PMIDS)}


def test_an_undecodable_file_fails_the_refresh_and_keeps_the_previous_snapshot(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A file that is not UTF-8 fails with `invalid_utf8`, and nothing changes."""
    first = _run(database_engine, unit_of_work_factory, rule_files())

    failed = _run(
        database_engine,
        unit_of_work_factory,
        rule_files({RETRACTED_PUBLICATIONS_PATH: b"PMID:1\n\xff\n"}),
    )

    assert failed.status == JobStatus.FAILED
    assert failed.progress["failure_code"] == "invalid_utf8"
    assert _stored(session_factory) == {_snapshot_id(first): (True, FIXTURE_PMIDS)}


def test_the_same_files_from_a_newer_commit_are_not_stored_again(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A go-site commit that leaves the six files alone creates no snapshot.

    The snapshot keeps the commit it was first read from, and the job reports the
    same counts as the refresh that stored it.
    """
    service = RulesRefreshService(TEST_ONTOLOGIES)
    first = apply_in_transaction(
        unit_of_work_factory,
        service,
        start_job(unit_of_work_factory, JobType.RULES_REFRESH, "go-site"),
        _document(rule_files(), "a" * 40),
    )
    second = apply_in_transaction(
        unit_of_work_factory,
        service,
        start_job(unit_of_work_factory, JobType.RULES_REFRESH, "go-site"),
        _document(rule_files(), "b" * 40),
    )

    assert (first.unchanged, second.unchanged) == (False, True)
    assert second.result["snapshot_id"] == first.result["snapshot_id"]
    assert second.result["source_revision"] == "a" * 40
    assert first.counts["database_count"] > 0
    assert second.counts == first.counts


def test_extension_namespaces_unknown_to_sabe_are_reported_as_warnings(
    database_engine: Engine, unit_of_work_factory: UnitOfWorkFactory
) -> None:
    """Only a namespace with no configured ontology and no database is reported.

    ZFA has an ontology source and MGI is a `db-xrefs.yaml` database, so only XAO
    is reported.
    """
    job = _run(
        database_engine,
        unit_of_work_factory,
        rule_files({EXTENSIONS_CONSTRAINTS_PATH: XAO_EXTENSIONS}),
        sources=sources_with(ontologies={"zfa": _ontology("ZFA")}),
    )

    assert job.status == JobStatus.SUCCEEDED
    assert job.result is not None
    assert job.result["unrecognized_extension_namespaces"] == ["XAO"]
    assert job.progress["unrecognized_extension_namespace_count"] == 1
    assert job.warnings == (XAO_WARNING,)


def test_the_warning_reflects_the_ontologies_configured_now(
    database_engine: Engine, unit_of_work_factory: UnitOfWorkFactory
) -> None:
    """Configuring an ontology clears its warning, even when the files are unchanged."""
    content = rule_files({EXTENSIONS_CONSTRAINTS_PATH: XAO_EXTENSIONS})
    zfa = _ontology("ZFA")
    before = _run(
        database_engine,
        unit_of_work_factory,
        content,
        sources=sources_with(ontologies={"zfa": zfa}),
    )

    after = _run(
        database_engine,
        unit_of_work_factory,
        content,
        sources=sources_with(ontologies={"zfa": zfa, "xao": _ontology("XAO")}),
    )

    assert before.warnings == (XAO_WARNING,)
    assert after.result is not None
    assert after.result["unchanged"] is True
    assert after.result["unrecognized_extension_namespaces"] == []
    assert after.warnings == ()
