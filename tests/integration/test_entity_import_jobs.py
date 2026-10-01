"""Verify the entity import worker task.

Covers job outcomes, failures, retries, and running a task again when its message
is delivered a second time.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

import pytest
from celery.exceptions import Retry
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.config import GpiEntitySourceSettings
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.entity_sources.http import (
    EntitySourceDocument,
    EntitySourceError,
)
from standard_annotation_backend.entity_sources.registry import (
    ConfiguredEntitySource,
    EntitySourceRegistry,
)
from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    EntityCatalogSnapshotRecord,
    EntityMembershipRecord,
    EntityStagingRecord,
    JobRecord,
)
from standard_annotation_backend.persistence.repositories.entities import (
    EntityRepository,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.entity_import_service import (
    EntityImportService,
)
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.workers import tasks

SOURCE_URL = "https://example.org/entities.gpi"
SOURCE_TEXT = (
    "!gpi-version: 2.0\n"
    "!generated-by: WorkerTest\n"
    "!date-generated: 2026-09-29\n"
    "MGI:1\tGene1\tProtein\t\tSO:0001217\tNCBITaxon:9606\t\tMGI:1\t\t\t\n"
)
SOURCE_SHA = sha256(SOURCE_TEXT.encode()).hexdigest()
REGISTRY = EntitySourceRegistry({"mgi": GpiEntitySourceSettings(url=SOURCE_URL)})


class RecordingSource:
    """Fake entity source client that returns a fixed document and counts fetches."""

    def __init__(
        self, *, error: Exception | None = None, text: str = SOURCE_TEXT
    ) -> None:
        self.calls = 0
        self.error = error
        self.text = text

    def fetch(self, source: ConfiguredEntitySource) -> EntitySourceDocument:
        """Return the fixed source or raise the configured failure."""
        self.calls += 1
        assert source == ConfiguredEntitySource(key="mgi", format="gpi", url=SOURCE_URL)
        if self.error is not None:
            raise self.error
        return EntitySourceDocument(
            source_key=source.key,
            source_url=source.url,
            source_checksum=sha256(self.text.encode()).hexdigest(),
            fetched_at=datetime(2026, 9, 29, tzinfo=UTC),
            text=self.text,
        )


@dataclass(frozen=True, slots=True)
class StoredJob:
    """Job fields read back from the database for assertions."""

    status: JobStatus
    parameters: dict[str, object]
    progress: dict[str, object]
    warnings: list[str]
    result: dict[str, object] | None
    error: str | None


def _create_job(
    jobs: JobService,
    *,
    source: str = "mgi",
    parameters: dict[str, object] | None = None,
) -> UUID:
    """Create a queued entity import job and return its ID.

    The job requests `source`, unless `parameters` is given to replace its parameters.
    """
    job = jobs.create(
        job_type=JobType.ENTITY_IMPORT,
        requested_by="curator",
        parameters=parameters or {"source_key": source},
    )
    return job.job_id


def _read_job(jobs: JobService, job_id: UUID) -> StoredJob:
    """Read a job's stored fields directly from the database, bypassing the HTTP API."""
    with jobs._unit_of_work_factory() as unit_of_work:
        record = unit_of_work.jobs.get(job_id)
        assert record is not None
        return StoredJob(
            status=JobStatus(record.status),
            parameters=dict(record.parameters),
            progress=dict(record.progress),
            warnings=list(record.warnings),
            result=None if record.result is None else dict(record.result),
            error=record.error,
        )


@contextmanager
def _runtime(
    engine: Engine,
    jobs: JobService,
    imports: object,
    source: object,
    registry: EntitySourceRegistry = REGISTRY,
) -> Iterator[object]:
    """Yield a stand-in worker runtime holding the given services and source."""
    yield SimpleNamespace(
        engine=engine,
        jobs=jobs,
        entity_imports=imports,
        entity_sources=registry,
        entity_source_client=source,
    )


def _install_runtime(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    jobs: JobService,
    imports: object,
    source: object,
    *,
    registry: EntitySourceRegistry = REGISTRY,
) -> None:
    """Make the worker task use a runtime built from the given services and source."""
    monkeypatch.setattr(
        tasks,
        "worker_runtime",
        lambda: _runtime(engine, jobs, imports, source, registry),
    )


def _staging_count(session_factory: sessionmaker[Session], job_id: UUID) -> int:
    """Return how many staged rows a job has."""
    with session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(EntityStagingRecord)
            .where(EntityStagingRecord.job_id == job_id)
        )
        return int(count or 0)


def _audit_events(
    session_factory: sessionmaker[Session],
    action: AuditAction,
    job_id: UUID | None = None,
) -> list[AuditEventRecord]:
    """Return audit events with `action`, optionally only those for one job."""
    statement = select(AuditEventRecord).where(AuditEventRecord.action == action)
    if job_id is not None:
        statement = statement.where(AuditEventRecord.job_id == job_id)
    with session_factory() as session:
        return list(session.scalars(statement))


def _memberships(session_factory: sessionmaker[Session]) -> list[tuple[str, str]]:
    """Return every active identifier with the source that supplies it."""
    with session_factory() as session:
        return [
            (row.db_object_id, row.source_key)
            for row in session.execute(
                select(
                    EntityMembershipRecord.db_object_id,
                    EntityMembershipRecord.source_key,
                ).order_by(EntityMembershipRecord.db_object_id)
            )
        ]


def _retry_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the import task raise Celery's `Retry` instead of scheduling a retry."""

    def retry(*, exc: Exception, countdown: int) -> None:
        assert countdown == 30
        raise Retry(exc=exc)

    monkeypatch.setattr(tasks.run_entity_import, "retry", retry)


@pytest.fixture
def entity_import_services(
    unit_of_work_factory: UnitOfWorkFactory,
) -> tuple[JobService, EntityImportService]:
    """Provide real job and entity import services."""
    return JobService(unit_of_work_factory), EntityImportService(unit_of_work_factory)


def test_entity_import_job_records_progress_and_result(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    entity_import_services: tuple[JobService, EntityImportService],
    integration_api_client: TestClient,
) -> None:
    """A successful import records its final progress and publication result.

    The job status endpoint returns the same result.
    """
    jobs, imports = entity_import_services
    job_id = _create_job(jobs)
    source = RecordingSource()
    _install_runtime(monkeypatch, database_engine, jobs, imports, source)

    tasks.run_entity_import.run(str(job_id))

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.parameters == {"source_key": "mgi"}
    assert stored.progress == {
        "phase": "completed",
        "source_record_count": 1,
        "active_identifier_count": 1,
        "added_count": 1,
        "retained_count": 0,
        "removed_count": 0,
        "warning_count": 0,
        "removal_impact_count": 0,
    }
    assert stored.warnings == []
    assert stored.result is not None
    assert stored.result["source_checksum"] == SOURCE_SHA
    assert stored.result["source_key"] == "mgi"
    assert stored.result["warning_count"] == 0
    response = integration_api_client.get(f"/jobs/{job_id}")
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "succeeded"
    assert response.json()["result"] == stored.result
    assert response.json()["result"]["warnings"] == []
    assert source.calls == 1


@pytest.mark.parametrize(
    ("failure_code", "failure_type"),
    [
        ("timeout", "EntitySourceError"),
        ("header", "GpiParseError"),
        ("row_validation", "GpiParseError"),
        ("catalog_collision", "EntityCatalogCollisionError"),
    ],
)
def test_entity_import_domain_failure_is_terminal_and_cleans_staging(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    entity_import_services: tuple[JobService, EntityImportService],
    seed_active_subjects: Callable[..., None],
    integration_api_client: TestClient,
    failure_code: str,
    failure_type: str,
) -> None:
    """Expected import failures fail the job with a generic message and a failure code.

    The failures are an unreachable source, an empty download, a row containing a
    NUL character, and an identifier that another source already supplies. Staged
    data is removed, the active catalogs are kept, and the logs omit source details
    such as identifiers, row content, and checksums.
    """
    log_records: list[str] = []

    def record_log(message: str, *args: object) -> None:
        log_records.append(message % args)

    monkeypatch.setattr(tasks.logger, "error", record_log)
    jobs, imports = entity_import_services
    if failure_code == "catalog_collision":
        seed_active_subjects("MGI:1")
    else:
        _install_runtime(monkeypatch, database_engine, jobs, imports, RecordingSource())
        tasks.run_entity_import.run(str(_create_job(jobs)))
    memberships_before = _memberships(session_factory)
    if failure_code == "timeout":
        source = RecordingSource(error=EntitySourceError("timeout"))
    elif failure_code == "header":
        source = RecordingSource(text="")
    elif failure_code == "row_validation":
        source = RecordingSource(text=SOURCE_TEXT.replace("Protein", "Pro\x00tein"))
    else:
        source = RecordingSource()
    _install_runtime(monkeypatch, database_engine, jobs, imports, source)
    job_id = _create_job(jobs)

    tasks.run_entity_import.run(str(job_id))

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.FAILED
    assert stored.error == "Entity import failed"
    assert stored.progress["failure_code"] == failure_code
    assert stored.progress["phase"] == "failed"
    response = integration_api_client.get(f"/jobs/{job_id}")
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["progress"]["failure_code"] == failure_code
    assert response.json()["error"] == "Entity import failed"
    assert "MGI:1" not in response.text
    audits = _audit_events(session_factory, AuditAction.JOB_FAILED, job_id)
    assert len(audits) == 1
    assert audits[0].details["failure_code"] == failure_code
    assert _staging_count(session_factory, job_id) == 0
    assert _memberships(session_factory) == memberships_before
    logs = "\n".join(log_records)
    assert str(job_id) in logs
    assert f"failure_type={failure_type}" in logs
    assert "MGI:1" not in logs
    assert "Gene1" not in logs
    assert sha256(source.text.encode()).hexdigest() not in logs


def test_entity_import_cleanup_failure_rolls_back_terminal_state_until_redelivery(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    entity_import_services: tuple[JobService, EntityImportService],
    seed_active_subjects: Callable[..., None],
) -> None:
    """A failed cleanup of staging data also rolls back the job's failure record.

    The job stays running and the task retries. When it runs again, the job is
    marked failed and its staging data is removed together. Running the task for
    the finished job once more changes nothing and does not contact the source.
    """
    jobs, imports = entity_import_services
    seed_active_subjects("MGI:1")
    job_id = _create_job(jobs)
    source = RecordingSource()
    original_cleanup = EntityRepository.cleanup_terminal_staging
    cleanup_failures = 1

    def fail_cleanup_once(repository: EntityRepository, received_job_id: UUID) -> None:
        nonlocal cleanup_failures
        if cleanup_failures:
            cleanup_failures -= 1
            raise RuntimeError("cleanup storage unavailable")
        original_cleanup(repository, received_job_id)

    monkeypatch.setattr(EntityRepository, "cleanup_terminal_staging", fail_cleanup_once)
    _retry_immediately(monkeypatch)
    _install_runtime(monkeypatch, database_engine, jobs, imports, source)

    with pytest.raises(Retry):
        tasks.run_entity_import.run(str(job_id))

    after_rollback = _read_job(jobs, job_id)
    assert after_rollback.status == JobStatus.RUNNING
    assert "failure_code" not in after_rollback.progress
    assert _staging_count(session_factory, job_id) == 1
    assert _audit_events(session_factory, AuditAction.JOB_FAILED, job_id) == []

    tasks.run_entity_import.run(str(job_id))

    terminal = _read_job(jobs, job_id)
    assert terminal.status == JobStatus.FAILED
    assert terminal.error == "Entity import failed"
    assert terminal.progress["failure_code"] == "catalog_collision"
    assert _staging_count(session_factory, job_id) == 0
    assert len(_audit_events(session_factory, AuditAction.JOB_FAILED, job_id)) == 1
    assert source.calls == 1

    tasks.run_entity_import.run(str(job_id))

    assert _read_job(jobs, job_id) == terminal
    assert len(_audit_events(session_factory, AuditAction.JOB_FAILED, job_id)) == 1
    assert source.calls == 1


def test_entity_import_job_redelivery_recovers_published_result(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    entity_import_services: tuple[JobService, EntityImportService],
) -> None:
    """After publication commits but recording success fails, the retry finishes it.

    The job succeeds with the stored publication result, without fetching the
    source or publishing again.
    """
    jobs, imports = entity_import_services
    job_id = _create_job(jobs)
    source = RecordingSource()
    original_succeed = jobs.succeed
    failures = 1

    def fail_once(
        received_job_id: UUID,
        *,
        result: dict[str, object],
        artifact_uri: str | None = None,
    ) -> Job:
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("connection dropped after publication")
        return original_succeed(
            received_job_id, result=result, artifact_uri=artifact_uri
        )

    monkeypatch.setattr(jobs, "succeed", fail_once)
    _retry_immediately(monkeypatch)
    _install_runtime(monkeypatch, database_engine, jobs, imports, source)

    with pytest.raises(Retry):
        tasks.run_entity_import.run(str(job_id))
    assert _read_job(jobs, job_id).status == JobStatus.RUNNING

    tasks.run_entity_import.run(str(job_id))

    stored = _read_job(jobs, job_id)
    published = _audit_events(session_factory, AuditAction.ENTITY_CATALOG_PUBLISHED)
    assert stored.status == JobStatus.SUCCEEDED
    assert len(published) == 1
    assert stored.result is not None
    assert stored.result["snapshot_id"] == published[0].details["snapshot_id"]
    assert stored.result["source_checksum"] == SOURCE_SHA
    assert stored.result["added_count"] == 1
    assert stored.result["warning_count"] == 0
    assert source.calls == 1
    assert _memberships(session_factory) == [("MGI:1", "mgi")]


def test_entity_import_redelivery_publishes_committed_staging_without_source_access(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    entity_import_services: tuple[JobService, EntityImportService],
) -> None:
    """After a publication failure, rerunning the task publishes the staged catalog.

    The interrupted job stays running with its staged data, and the rerun does not
    contact the source again.
    """
    jobs, imports = entity_import_services
    job_id = _create_job(jobs)
    source = RecordingSource()
    _install_runtime(monkeypatch, database_engine, jobs, imports, source)

    def outage(*args: object, **kwargs: object) -> None:
        raise RuntimeError("publication unavailable")

    _retry_immediately(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(EntityRepository, "publish", outage)
        with pytest.raises(Retry):
            tasks.run_entity_import.run(str(job_id))
    interrupted = _read_job(jobs, job_id)
    assert interrupted.status == JobStatus.RUNNING
    assert interrupted.progress["phase"] == "publishing"
    with session_factory() as session:
        snapshot_id = session.scalar(select(EntityCatalogSnapshotRecord.snapshot_id))
        assert snapshot_id is not None
        assert (
            session.scalar(select(func.count()).select_from(EntityStagingRecord)) == 1
        )
    source.error = EntitySourceError("http_status")

    tasks.run_entity_import.run(str(job_id))

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.result is not None
    assert stored.result["snapshot_id"] == str(snapshot_id)
    assert source.calls == 1
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(EntityStagingRecord)) == 0
        )
        assert list(session.scalars(select(EntityMembershipRecord.db_object_id))) == [
            "MGI:1"
        ]


@pytest.mark.parametrize("changed", ["source_key", "missing_row", "entity"])
def test_entity_import_redelivery_rejects_incompatible_or_incomplete_staging_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    entity_import_services: tuple[JobService, EntityImportService],
    changed: str,
) -> None:
    """Rerunning a task whose staged data no longer matches the job fails the job.

    The job fails with `candidate_conflict` without fetching the source, and the
    previous catalog stays active. Mismatches include staging for another source,
    a missing row, and an altered row.
    """
    jobs, imports = entity_import_services
    source = RecordingSource()
    previous = _create_job(jobs)
    _install_runtime(monkeypatch, database_engine, jobs, imports, source)
    tasks.run_entity_import.run(str(previous))
    job_id = _create_job(jobs)
    document = source.fetch(REGISTRY.source("mgi"))
    imports.stage(job_id=job_id, source_key="mgi", catalog=tasks.parse_gpi(document))
    with session_factory() as session:
        job = session.get(JobRecord, job_id)
        row = session.scalar(
            select(EntityStagingRecord).where(EntityStagingRecord.job_id == job_id)
        )
        assert job is not None and row is not None
        if changed == "source_key":
            parameters: dict[str, object] = {"source_key": "rgd"}
            job.parameters = parameters
        elif changed == "missing_row":
            session.execute(
                delete(EntityStagingRecord).where(EntityStagingRecord.job_id == job_id)
            )
        else:
            row.entity = {**row.entity, "db_object_name": "changed"}
        session.commit()
    source.calls = 0
    source.error = EntitySourceError("http_status")

    tasks.run_entity_import.run(str(job_id))

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.FAILED
    assert stored.progress["failure_code"] == "candidate_conflict"
    assert source.calls == 0
    with session_factory() as session:
        active = session.scalar(
            select(EntityCatalogSnapshotRecord).where(
                EntityCatalogSnapshotRecord.active.is_(True)
            )
        )
        assert active is not None and active.job_id == previous
        assert list(session.scalars(select(EntityMembershipRecord.db_object_id))) == [
            "MGI:1"
        ]
        assert (
            session.scalar(select(func.count()).select_from(EntityStagingRecord)) == 0
        )


def test_unchanged_source_succeeds_without_staging_or_publication(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    entity_import_services: tuple[JobService, EntityImportService],
) -> None:
    """A source matching the active catalog by URL and content completes unchanged.

    No new snapshot or publication audit event is created.
    """
    jobs, imports = entity_import_services
    source = RecordingSource()
    _install_runtime(monkeypatch, database_engine, jobs, imports, source)
    first = _create_job(jobs)
    tasks.run_entity_import.run(str(first))
    first_result = _read_job(jobs, first).result
    assert first_result is not None
    second = _create_job(jobs)

    tasks.run_entity_import.run(str(second))

    stored = _read_job(jobs, second)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.result == {
        "source_key": "mgi",
        "snapshot_id": first_result["snapshot_id"],
        "source_checksum": SOURCE_SHA,
        "unchanged": True,
    }
    assert stored.progress == {"phase": "completed", "unchanged": True}
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(EntityCatalogSnapshotRecord)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEventRecord)
                .where(AuditEventRecord.action == AuditAction.ENTITY_CATALOG_PUBLISHED)
            )
            == 1
        )


@pytest.mark.parametrize("changed", ["url", "content"])
def test_changed_url_or_content_publishes_new_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    entity_import_services: tuple[JobService, EntityImportService],
    changed: str,
) -> None:
    """A refresh with a changed URL or changed content publishes a new snapshot.

    Moving a source to a new URL publishes even when the content is identical, and
    new content at the same URL is never reported as unchanged.
    """
    jobs, imports = entity_import_services
    _install_runtime(monkeypatch, database_engine, jobs, imports, RecordingSource())
    tasks.run_entity_import.run(str(_create_job(jobs)))
    if changed == "url":
        expected_url, text = "https://example.org/moved/entities.gpi", SOURCE_TEXT
    else:
        expected_url, text = SOURCE_URL, SOURCE_TEXT.replace("Gene1", "Gene1b")

    class ChangedSource(RecordingSource):
        def fetch(self, source: ConfiguredEntitySource) -> EntitySourceDocument:
            self.calls += 1
            return EntitySourceDocument(
                source_key=source.key,
                source_url=source.url,
                source_checksum=sha256(text.encode()).hexdigest(),
                fetched_at=datetime(2026, 9, 30, tzinfo=UTC),
                text=text,
            )

    _install_runtime(
        monkeypatch,
        database_engine,
        jobs,
        imports,
        ChangedSource(),
        registry=EntitySourceRegistry(
            {"mgi": GpiEntitySourceSettings(url=expected_url)}
        ),
    )
    job_id = _create_job(jobs)

    tasks.run_entity_import.run(str(job_id))

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.result is not None and "unchanged" not in stored.result
    assert stored.result["source_url"] == expected_url
    assert stored.result["source_checksum"] == sha256(text.encode()).hexdigest()
    assert stored.result["retained_count"] == 1
    published = _audit_events(session_factory, AuditAction.ENTITY_CATALOG_PUBLISHED)
    assert len(published) == 2
    with session_factory() as session:
        active = session.scalar(
            select(EntityCatalogSnapshotRecord).where(
                EntityCatalogSnapshotRecord.active.is_(True)
            )
        )
        assert active is not None and active.job_id == job_id


def test_matching_catalog_of_another_source_does_not_make_import_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    entity_import_services: tuple[JobService, EntityImportService],
) -> None:
    """Only the same source's active catalog can make an import unchanged.

    Another source's active catalog with the same URL and checksum is ignored, and
    the import publishes this source's catalog.
    """
    jobs, imports = entity_import_services
    other_job = jobs.create(
        job_type=JobType.ENTITY_IMPORT,
        requested_by="curator",
        parameters={"source_key": "rgd"},
    ).job_id
    jobs.start(other_job)
    other_text = SOURCE_TEXT.replace("MGI:1", "RGD:1")
    imports.stage(
        job_id=other_job,
        source_key="rgd",
        catalog=tasks.parse_gpi(
            EntitySourceDocument(
                source_key="rgd",
                source_url=SOURCE_URL,
                source_checksum=SOURCE_SHA,
                fetched_at=datetime(2026, 9, 29, tzinfo=UTC),
                text=other_text,
            )
        ),
    )
    imports.publish(job_id=other_job, actor_id="curator")
    source = RecordingSource()
    _install_runtime(monkeypatch, database_engine, jobs, imports, source)
    job_id = _create_job(jobs)

    tasks.run_entity_import.run(str(job_id))

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.result is not None and "unchanged" not in stored.result
    assert stored.result["source_key"] == "mgi"
    assert stored.result["added_count"] == 1
    assert _memberships(session_factory) == [("MGI:1", "mgi"), ("RGD:1", "rgd")]


def test_source_removed_before_execution_fails_and_keeps_catalog(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    session_factory: sessionmaker[Session],
    entity_import_services: tuple[JobService, EntityImportService],
) -> None:
    """A job whose source key was removed from the registry fails with `unknown_source`.

    The existing catalog is kept.
    """
    jobs, imports = entity_import_services
    source = RecordingSource()
    _install_runtime(monkeypatch, database_engine, jobs, imports, source)
    tasks.run_entity_import.run(str(_create_job(jobs)))
    _install_runtime(
        monkeypatch,
        database_engine,
        jobs,
        imports,
        source,
        registry=EntitySourceRegistry({}),
    )
    job_id = _create_job(jobs)

    tasks.run_entity_import.run(str(job_id))

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.FAILED
    assert stored.error == "Entity import failed"
    assert stored.progress["failure_code"] == "unknown_source"
    assert source.calls == 1
    with session_factory() as session:
        assert list(session.scalars(select(EntityMembershipRecord.db_object_id))) == [
            "MGI:1"
        ]


def test_orphaned_running_job_without_staging_completes_when_redelivered(
    monkeypatch: pytest.MonkeyPatch,
    database_engine: Engine,
    entity_import_services: tuple[JobService, EntityImportService],
) -> None:
    """A job left running with no staging imports and succeeds when run again."""
    jobs, imports = entity_import_services
    job_id = _create_job(jobs)
    assert jobs.start(job_id).status is JobStatus.RUNNING
    source = RecordingSource()
    _install_runtime(monkeypatch, database_engine, jobs, imports, source)

    tasks.run_entity_import.run(str(job_id))

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.result is not None
    assert stored.result["source_checksum"] == SOURCE_SHA
    assert source.calls == 1
