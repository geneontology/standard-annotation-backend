"""Verify that rules refreshes activate the go-site files GO Rules read together."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from refresh_helpers import (
    REVISION,
    RULES_LOCATOR,
    TEST_SOURCES,
    FakeFetchers,
    build_runner,
    ignore_progress,
    rule_files,
    source_document,
    sources_with,
    start_job,
)
from seeding import create_job
from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import RefreshOutcome, SourceDocument
from standard_annotation_backend.go_site.rule_files import (
    EXTENSIONS_CONSTRAINTS_PATH,
    GOREFS_PATH,
    RETRACTED_PUBLICATIONS_PATH,
)
from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    JobRecord,
    RetractedPublicationRecord,
    RulesSnapshotRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services import rules_refresh_service
from standard_annotation_backend.services.job_service import Job
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
) -> JobRecord:
    """Run one rules refresh job over packed `content` and return the finished job."""
    job_id = create_job(
        factory,
        job_type=JobType.RULES_REFRESH,
        requested_by="curator",
        parameters={"source_key": "go-site"},
    ).job_id
    fetchers = FakeFetchers({"go-site": content})
    build_runner(engine, factory, fetchers, sources=sources).run(job_id)
    with factory() as uow:
        record = uow.jobs.get(job_id)
        assert record is not None
        uow.jobs.session.expunge(record)
        return record


def _snapshot_id(job: JobRecord) -> UUID:
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
            for record in session.scalars(select(RulesSnapshotRecord))
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
    assert job.warnings == []
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
    session_factory: sessionmaker[Session],
) -> None:
    """A go-site commit that leaves the six files alone creates no snapshot.

    The snapshot keeps the commit it was first read from.
    """
    service = RulesRefreshService(unit_of_work_factory, TEST_SOURCES)
    first = service.apply(
        start_job(unit_of_work_factory, JobType.RULES_REFRESH, "go-site"),
        _document(rule_files(), "a" * 40),
        ignore_progress,
    )
    second = service.apply(
        start_job(unit_of_work_factory, JobType.RULES_REFRESH, "go-site"),
        _document(rule_files(), "b" * 40),
        ignore_progress,
    )

    assert (first.unchanged, second.unchanged) == (False, True)
    assert second.result["snapshot_id"] == first.result["snapshot_id"]
    assert second.result["source_revision"] == "a" * 40
    assert len(_stored(session_factory)) == 1


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(
            {
                "databases": [],
                "go_refs": [],
                "group_shorthands": ["OLD"],
                "eco_usage_constraints": [],
                "extension_constraints": [],
            },
            id="readable",
        ),
        pytest.param({"format": "from an earlier release"}, id="unreadable"),
    ],
)
def test_unchanged_files_read_differently_by_an_earlier_release_are_stored_again(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    stored: dict[str, object],
) -> None:
    """Identical files are activated again when the stored rule data differs.

    A SABE release that parses or stores the files differently replaces the old
    snapshot on its next refresh, even when this release cannot read the stored
    form at all.
    """
    first = _run(database_engine, unit_of_work_factory, rule_files())
    with session_factory.begin() as session:
        session.execute(
            update(RulesSnapshotRecord)
            .where(RulesSnapshotRecord.snapshot_id == _snapshot_id(first))
            .values(rule_data=stored)
        )

    second = _run(database_engine, unit_of_work_factory, rule_files())

    assert second.status == JobStatus.SUCCEEDED
    assert second.result is not None
    assert second.result["unchanged"] is False
    assert second.result["group_count"] == 2
    assert _stored(session_factory) == {
        _snapshot_id(first): (False, FIXTURE_PMIDS),
        _snapshot_id(second): (True, FIXTURE_PMIDS),
    }


def test_unchanged_files_with_different_stored_pmids_are_stored_again(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Identical files are activated again when the stored PMIDs differ."""
    first = _run(database_engine, unit_of_work_factory, rule_files())
    with session_factory.begin() as session:
        session.execute(
            delete(RetractedPublicationRecord).where(
                RetractedPublicationRecord.snapshot_id == _snapshot_id(first),
                RetractedPublicationRecord.pmid == "PMID:10026173",
            )
        )

    second = _run(database_engine, unit_of_work_factory, rule_files())

    assert second.result is not None
    assert second.result["unchanged"] is False
    assert _stored(session_factory)[_snapshot_id(second)] == (True, FIXTURE_PMIDS)


def test_activation_keeps_the_replaced_snapshot_and_deletes_older_ones(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """The snapshot an activation replaces keeps its data; older ones are deleted.

    A reader that resolved the replaced snapshot just before the activation can
    therefore still read it.
    """
    jobs = [
        _run(
            database_engine,
            unit_of_work_factory,
            rule_files({RETRACTED_PUBLICATIONS_PATH: f"{pmid}\n".encode()}),
        )
        for pmid in ("PMID:1", "PMID:2", "PMID:3")
    ]

    assert _stored(session_factory) == {
        _snapshot_id(jobs[1]): (False, {"PMID:2"}),
        _snapshot_id(jobs[2]): (True, {"PMID:3"}),
    }


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
    assert job.warnings == [XAO_WARNING]


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

    assert before.warnings == [XAO_WARNING]
    assert after.result is not None
    assert after.result["unchanged"] is True
    assert after.result["unrecognized_extension_namespaces"] == []
    assert after.warnings == []


def test_concurrent_refreshes_of_the_same_files_activate_one_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two jobs that apply the same files at once store and audit them once.

    The service's unlocked check is disabled so that both jobs reach the
    repository, where the check made under the rules refresh lock decides.
    """
    monkeypatch.setattr(rules_refresh_service, "same_rule_files", lambda *_: False)
    service = RulesRefreshService(unit_of_work_factory, TEST_SOURCES)
    jobs = [
        start_job(unit_of_work_factory, JobType.RULES_REFRESH, "go-site")
        for _ in range(2)
    ]
    document = _document(rule_files(), REVISION)
    ready = Barrier(2)

    def apply(job: Job) -> RefreshOutcome:
        ready.wait(timeout=3)
        return service.apply(job, document, ignore_progress)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(apply, jobs))

    assert sorted(outcome.unchanged for outcome in outcomes) == [False, True]
    assert len({outcome.result["snapshot_id"] for outcome in outcomes}) == 1
    assert len(_stored(session_factory)) == 1
    with session_factory() as session:
        audits = session.scalar(
            select(func.count())
            .select_from(AuditEventRecord)
            .where(AuditEventRecord.action == AuditAction.RULES_REFRESHED)
        )
    assert audits == 1
