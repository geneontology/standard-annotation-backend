"""Verify the lifecycle every ontology, rules, and entity snapshot shares.

Each kind stores a snapshot per refresh, skips content it already holds, keeps
the active snapshot and the one it replaced, and prunes the rest. Readers keep
reading the snapshots they resolved until their transaction ends.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import MappingProxyType

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from refresh_helpers import (
    GO_LOCATOR,
    GO_SITE_FIXTURES,
    TEST_SOURCES,
    FakeFetchers,
    OboTerm,
    apply_in_transaction,
    build_runner,
    go_document,
    gpi_bytes,
    obo,
    rule_files,
    source_document,
    start_job,
)
from seeding import SHIPPED_SOURCES, active_snapshot
from seeding import create_job as queue_job
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain import snapshots as snapshot_vocabulary
from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import (
    RULES_SOURCE_KEY,
    RefreshKindName,
    SourceDocument,
)
from standard_annotation_backend.domain.semantic_validation.facts import TermStatus
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.persistence.models import (
    OntologyTermRecord,
    ReferenceSnapshotRecord,
    RuleDataRecord,
    StoredEntityRecord,
)
from standard_annotation_backend.persistence.repositories import SnapshotPrunedError
from standard_annotation_backend.persistence.repositories.snapshots import (
    SnapshotRepository,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshService,
)

SHIPPED_ONTOLOGIES = SHIPPED_SOURCES.ontology_catalog()
WAIT_SECONDS = 30
"""How long a refresh may take while a reader holds its snapshots."""


def _run(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    job_type: JobType,
    source_key: str,
    content: bytes,
) -> Job:
    """Queue and run one refresh job with `content` as its source, and return it."""
    job = queue_job(
        unit_of_work_factory,
        job_type=job_type,
        requested_by="scheduler",
        parameters={"source_key": source_key},
    )
    build_runner(
        database_engine, unit_of_work_factory, FakeFetchers({source_key: content})
    ).run(job.job_id)
    return JobService(unit_of_work_factory).find(job.job_id)


RETRACTED = (GO_SITE_FIXTURES / "retracted-publications.txt").read_bytes()

CASES = [
    (JobType.ONTOLOGY_REFRESH, "go", lambda n: obo(OboTerm(f"GO:000000{n}"))),
    (
        JobType.RULES_REFRESH,
        "go-site",
        lambda n: rule_files(
            {
                "metadata/retracted-publications.txt": RETRACTED
                + f"PMID:{9000 + n}\n".encode()
            }
        ),
    ),
    (JobType.ENTITY_REFRESH, "mgi", lambda n: gpi_bytes(f"MGI:MGI:{n}")),
]
CASE_IDS = ["ontology", "rules", "entity"]

KINDS = {
    JobType.ONTOLOGY_REFRESH: SnapshotKind.ONTOLOGY,
    JobType.RULES_REFRESH: SnapshotKind.RULES,
    JobType.ENTITY_REFRESH: SnapshotKind.ENTITY,
}
DATA_RECORDS: dict[
    SnapshotKind, type[OntologyTermRecord | RuleDataRecord | StoredEntityRecord]
] = {
    SnapshotKind.ONTOLOGY: OntologyTermRecord,
    SnapshotKind.RULES: RuleDataRecord,
    SnapshotKind.ENTITY: StoredEntityRecord,
}


def _annotation(db_object_id: str = "UniProtKB:P12345") -> Annotation:
    """Return a valid annotation of `GO:0006915`, a seeded GO term."""
    return Annotation.model_validate(
        {
            "db_object_id": db_object_id,
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0006915",
            "references": ["PMID:1"],
            "evidence_type": "ECO:0000314",
            "annotation_date": "2026-09-11",
            "assigned_by": "GO_Central",
        }
    )


def _snapshot_of(
    session_factory: sessionmaker[Session], job: Job
) -> ReferenceSnapshotRecord:
    """Return the snapshot a job stored, detached from its session."""
    with session_factory() as session:
        return session.scalars(
            select(ReferenceSnapshotRecord).where(
                ReferenceSnapshotRecord.job_id == job.job_id
            )
        ).one()


def _data_row_count(
    session_factory: sessionmaker[Session], kind: SnapshotKind, record_id: object
) -> int:
    """Return how many data rows of its kind a snapshot still has."""
    model = DATA_RECORDS[kind]
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.snapshot_id == record_id)
            )
            or 0
        )


def test_a_new_revision_with_identical_bytes_is_unchanged(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A new commit of an ontology file with the same bytes is not loaded again."""
    service = OntologyRefreshService(TEST_SOURCES)
    content = obo(OboTerm("GO:0000001"))
    apply_in_transaction(
        unit_of_work_factory,
        service,
        start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go"),
        go_document(content),
    )

    outcome = apply_in_transaction(
        unit_of_work_factory,
        service,
        start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go"),
        source_document(
            "go",
            content,
            source_type="github",
            source_locator=GO_LOCATOR,
            source_revision="b" * 40,
        ),
    )

    assert outcome.unchanged


def test_a_new_revision_of_an_entity_file_with_identical_bytes_is_unchanged(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A new commit of an entity file with the same bytes is not loaded again."""
    service = EntityRefreshService()
    content = gpi_bytes("MGI:MGI:1")

    def document(revision: str) -> SourceDocument:
        return source_document(
            "mgi",
            content,
            source_type="github",
            source_locator="github:example/entities:mgi.gpi",
            source_revision=revision,
        )

    first = apply_in_transaction(
        unit_of_work_factory,
        service,
        start_job(unit_of_work_factory, JobType.ENTITY_REFRESH, "mgi"),
        document("a" * 40),
    )

    second = apply_in_transaction(
        unit_of_work_factory,
        service,
        start_job(unit_of_work_factory, JobType.ENTITY_REFRESH, "mgi"),
        document("b" * 40),
    )

    assert not first.unchanged
    assert second.unchanged
    assert second.result["snapshot_id"] == first.result["snapshot_id"]


@pytest.mark.parametrize(("job_type", "source_key", "content"), CASES, ids=CASE_IDS)
def test_a_higher_reader_version_loads_the_same_document_again(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    job_type: JobType,
    source_key: str,
    content: Callable[[int], bytes],
) -> None:
    """A release with a newer reader for a kind reloads that kind's unchanged file."""
    _run(database_engine, unit_of_work_factory, job_type, source_key, content(1))
    kind = RefreshKindName(KINDS[job_type].value)
    monkeypatch.setattr(
        snapshot_vocabulary,
        "READER_VERSIONS",
        MappingProxyType(
            {
                **snapshot_vocabulary.READER_VERSIONS,
                kind: snapshot_vocabulary.READER_VERSIONS[kind] + 1,
            }
        ),
    )

    job = _run(database_engine, unit_of_work_factory, job_type, source_key, content(1))

    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None and job.result["unchanged"] is False


@pytest.mark.parametrize(("job_type", "source_key", "content"), CASES, ids=CASE_IDS)
def test_each_job_keeps_the_active_snapshot_and_its_predecessor(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    job_type: JobType,
    source_key: str,
    content: Callable[[int], bytes],
) -> None:
    """A third snapshot prunes the first and keeps the second and third."""
    kind = KINDS[job_type]
    jobs = [
        _run(database_engine, unit_of_work_factory, job_type, source_key, content(n))
        for n in (1, 2, 3)
    ]

    first, second, third = (_snapshot_of(session_factory, job) for job in jobs)
    active = active_snapshot(session_factory, kind, source_key)
    assert first.pruned_at is not None
    assert _data_row_count(session_factory, kind, first.snapshot_id) == 0
    assert second.pruned_at is None
    assert _data_row_count(session_factory, kind, second.snapshot_id) > 0
    assert active is not None and active.snapshot_id == third.snapshot_id
    assert _data_row_count(session_factory, kind, third.snapshot_id) > 0


def _refresh_go(
    database_engine: Engine, unit_of_work_factory: UnitOfWorkFactory, n: int
) -> Job:
    """Run a GO refresh whose document differs for each `n`."""
    return _run(
        database_engine,
        unit_of_work_factory,
        JobType.ONTOLOGY_REFRESH,
        "go",
        obo(OboTerm(f"GO:000000{n}")),
    )


def test_the_replaced_snapshot_is_kept_whatever_its_activation_time(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Pruning keeps the snapshot the active one replaced, not the newest by clock.

    The first snapshot is given a later activation time than the second, as clock
    skew between workers could do. The third refresh still keeps the second.
    """
    first_job = _refresh_go(database_engine, unit_of_work_factory, 1)
    second_job = _refresh_go(database_engine, unit_of_work_factory, 2)
    second = _snapshot_of(session_factory, second_job)
    assert second.activated_at is not None
    with session_factory.begin() as session:
        session.execute(
            update(ReferenceSnapshotRecord)
            .where(ReferenceSnapshotRecord.job_id == first_job.job_id)
            .values(activated_at=second.activated_at + timedelta(hours=1))
        )

    third_job = _refresh_go(database_engine, unit_of_work_factory, 3)

    first = _snapshot_of(session_factory, first_job)
    second = _snapshot_of(session_factory, second_job)
    third = _snapshot_of(session_factory, third_job)
    kind = SnapshotKind.ONTOLOGY
    assert first.pruned_at is not None
    assert _data_row_count(session_factory, kind, first.snapshot_id) == 0
    assert second.pruned_at is None
    assert _data_row_count(session_factory, kind, second.snapshot_id) > 0
    active = active_snapshot(session_factory, kind, "go")
    assert active is not None and active.snapshot_id == third.snapshot_id


def test_a_reconfigured_source_does_not_keep_its_retired_snapshot(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A source's first snapshot after retirement replaced nothing, so it keeps none."""
    first_job = _refresh_go(database_engine, unit_of_work_factory, 1)
    with session_factory.begin() as session:
        SnapshotRepository(session).retire(SnapshotKind.ONTOLOGY, ["go"])

    second_job = _refresh_go(database_engine, unit_of_work_factory, 2)

    first = _snapshot_of(session_factory, first_job)
    assert first.pruned_at is not None
    assert (
        _data_row_count(session_factory, SnapshotKind.ONTOLOGY, first.snapshot_id) == 0
    )
    assert _snapshot_of(session_factory, second_job).replaced_snapshot_id is None


def test_a_reader_keeps_its_snapshot_through_two_activations_and_prunes(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    reference_data: Callable[..., None],
) -> None:
    """Refreshes never wait for a reader, and never prune what it is reading.

    Once the reader is done, the next refresh prunes its snapshot. Reading that
    snapshot again then raises an error instead of returning no data.
    """

    def refresh_go_twice() -> list[Job]:
        return [
            _run(
                database_engine,
                unit_of_work_factory,
                JobType.ONTOLOGY_REFRESH,
                "go",
                obo(OboTerm(f"GO:000000{n}")),
            )
            for n in (1, 2)
        ]

    # The reader closes before the executor so a refresh that waits on the
    # reader's locks is released and the test fails instead of hanging.
    with ThreadPoolExecutor(1) as executor, unit_of_work_factory() as reader:
        resolved = reader.snapshots.resolve(SHIPPED_ONTOLOGIES)
        jobs = executor.submit(refresh_go_twice).result(timeout=WAIT_SECONDS)
        (facts,) = reader.validation_facts.gather(
            [_annotation()], [()], SHIPPED_ONTOLOGIES, snapshots=resolved
        )

    assert [job.status for job in jobs] == [JobStatus.SUCCEEDED] * 2
    assert facts.terms["GO:0006915"].status is TermStatus.CURRENT
    _run(
        database_engine,
        unit_of_work_factory,
        JobType.ONTOLOGY_REFRESH,
        "go",
        obo(OboTerm("GO:0000003")),
    )
    with unit_of_work_factory() as unit_of_work, pytest.raises(SnapshotPrunedError):
        unit_of_work.validation_facts.gather(
            [_annotation()], [()], SHIPPED_ONTOLOGIES, snapshots=resolved
        )


def test_validation_reads_entity_facts_from_the_resolved_catalog(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    reference_data: Callable[..., None],
) -> None:
    """An entity removed by a newer catalog is still found by an earlier reader."""
    _run(
        database_engine,
        unit_of_work_factory,
        JobType.ENTITY_REFRESH,
        "mgi",
        gpi_bytes("MGI:MGI:1", "MGI:MGI:2"),
    )
    annotation = _annotation("MGI:MGI:2")

    # The reader closes before the executor so a refresh that waits on the
    # reader's locks is released and the test fails instead of hanging.
    with ThreadPoolExecutor(1) as executor, unit_of_work_factory() as reader:
        resolved = reader.snapshots.resolve(SHIPPED_ONTOLOGIES)
        replaced = executor.submit(
            _run,
            database_engine,
            unit_of_work_factory,
            JobType.ENTITY_REFRESH,
            "mgi",
            gpi_bytes("MGI:MGI:1"),
        ).result(timeout=WAIT_SECONDS)
        (pinned,) = reader.validation_facts.gather(
            [annotation], [()], SHIPPED_ONTOLOGIES, snapshots=resolved
        )
    with unit_of_work_factory() as unit_of_work:
        (current,) = unit_of_work.validation_facts.gather(
            [annotation], [()], SHIPPED_ONTOLOGIES
        )

    assert replaced.status is JobStatus.SUCCEEDED
    assert pinned.entity is not None
    assert current.entity is None


def test_undecodable_rule_data_from_an_older_reader_is_unavailable_until_refreshed(
    integration_api_client: TestClient,
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    reference_data: Callable[..., None],
    seed_active_subjects: Callable[..., None],
) -> None:
    """Rule data an earlier reader stored and this one cannot read gives 503.

    The same rule files are then loaded again because the stored data came from
    an older reader, and writes succeed.
    """
    # The go-site fixture files list MGI, but not UniProtKB, as a database.
    seed_active_subjects("MGI:MGI:1")
    payload = {
        "owning_group_id": "MGI",
        "annotation": _annotation("MGI:MGI:1").model_dump(mode="json"),
    }
    files = rule_files()
    loaded = _run(
        database_engine,
        unit_of_work_factory,
        JobType.RULES_REFRESH,
        RULES_SOURCE_KEY,
        files,
    )
    with session_factory.begin() as session:
        rules = session.scalars(
            select(ReferenceSnapshotRecord).where(
                ReferenceSnapshotRecord.kind == SnapshotKind.RULES,
                ReferenceSnapshotRecord.active.is_(True),
            )
        ).one()
        rules.reader_version = 0
        undecodable: dict[str, object] = {"unexpected": True}
        session.get_one(RuleDataRecord, rules.snapshot_id).data = undecodable

    unavailable = integration_api_client.post("/annotations", json=payload)
    reloaded = _run(
        database_engine,
        unit_of_work_factory,
        JobType.RULES_REFRESH,
        RULES_SOURCE_KEY,
        files,
    )
    created = integration_api_client.post("/annotations", json=payload)

    assert loaded.result is not None and loaded.result["unchanged"] is False
    assert unavailable.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert unavailable.json()["error"]["code"] == "reference_data_unavailable"
    assert reloaded.status is JobStatus.SUCCEEDED
    assert reloaded.result is not None and reloaded.result["unchanged"] is False
    assert created.status_code == status.HTTP_201_CREATED
