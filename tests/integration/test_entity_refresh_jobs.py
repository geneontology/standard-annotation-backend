"""Verify entity refresh jobs run through the shared refresh runner.

Covers job outcomes, failures, and running a job again when its message is
delivered a second time. Lifecycle rules shared by every kind are covered in
`test_refresh_runner.py`; Celery retry behavior is covered in
`tests/unit/test_worker_task_safety.py`.
"""

import gzip
from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from refresh_helpers import (
    TEST_SOURCES,
    FakeFetchers,
    build_runner,
    ignore_progress,
    sources_with_entities,
    stage_without_publishing,
    start_job,
)
from seeding import create_job
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    RefreshKindName,
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
from standard_annotation_backend.refresh import runner as refresh_runner
from standard_annotation_backend.refresh.fetchers import SourceError
from standard_annotation_backend.refresh.runner import RefreshRunner
from standard_annotation_backend.services import (
    entity_refresh_service as entity_refresh,
)
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import JobService

SOURCE_URL = "https://example.org/mgi.gpi"
SOURCE_TEXT = (
    "!gpi-version: 2.0\n"
    "!generated-by: WorkerTest\n"
    "!date-generated: 2026-09-29\n"
    "MGI:1\tGene1\tProtein\t\tSO:0001217\tNCBITaxon:9606\t\tMGI:1\t\t\t\n"
)
SOURCE_BYTES = SOURCE_TEXT.encode()
SOURCE_SHA = sha256(SOURCE_BYTES).hexdigest()
FAILED = "Entity refresh failed"


@dataclass(frozen=True, slots=True)
class StoredJob:
    """Job fields read back from the database for assertions."""

    status: JobStatus
    parameters: dict[str, object]
    progress: dict[str, object]
    warnings: list[str]
    result: dict[str, object] | None
    error: str | None


def _fetchers(content: bytes | Exception = SOURCE_BYTES) -> FakeFetchers:
    """Return fake fetchers that serve `content` for the `mgi` source."""
    return FakeFetchers({"mgi": content})


def _create_job(
    factory: UnitOfWorkFactory,
    *,
    source: str = "mgi",
    parameters: dict[str, object] | None = None,
) -> UUID:
    """Create a queued entity refresh job and return its ID.

    The job requests `source`, unless `parameters` is given to replace its parameters.
    """
    job = create_job(
        factory,
        job_type=JobType.ENTITY_REFRESH,
        requested_by="curator",
        parameters=parameters or {"source_key": source},
    )
    return job.job_id


def _read_job(jobs: JobService, job_id: UUID) -> StoredJob:
    """Read a job's stored fields directly from the database, bypassing the HTTP API."""
    record = jobs.find(job_id)
    return StoredJob(
        status=record.status,
        parameters=dict(record.parameters),
        progress=dict(record.progress),
        warnings=list(record.warnings),
        result=None if record.result is None else dict(record.result),
        error=record.error,
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


def _record_error_logs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture error logs from the runner and the entity kind as rendered text."""
    records: list[str] = []

    def record_log(message: str, *args: object) -> None:
        records.append(message % args)

    monkeypatch.setattr(refresh_runner.logger, "error", record_log)
    monkeypatch.setattr(entity_refresh.logger, "error", record_log)
    return records


@pytest.fixture
def jobs(unit_of_work_factory: UnitOfWorkFactory) -> JobService:
    """Provide the real job service."""
    return JobService(unit_of_work_factory)


@pytest.fixture
def entity_service(unit_of_work_factory: UnitOfWorkFactory) -> EntityRefreshService:
    """Provide the real entity refresh service."""
    return EntityRefreshService(unit_of_work_factory, TEST_SOURCES)


@pytest.fixture
def runner_for(
    database_engine: Engine, unit_of_work_factory: UnitOfWorkFactory
) -> Callable[..., RefreshRunner]:
    """Return a factory for runners around the given fake fetchers."""

    def build(fetchers: FakeFetchers, **kwargs: Any) -> RefreshRunner:
        return build_runner(database_engine, unit_of_work_factory, fetchers, **kwargs)

    return build


def test_entity_refresh_job_records_progress_and_result(
    unit_of_work_factory: UnitOfWorkFactory,
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    integration_api_client: TestClient,
) -> None:
    """A successful refresh records its final progress and publication result.

    The job status endpoint returns the same result.
    """
    job_id = _create_job(unit_of_work_factory)
    fetchers = _fetchers()

    runner_for(fetchers).run(job_id)

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
        "unchanged": False,
    }
    assert stored.warnings == []
    assert stored.result is not None
    assert stored.result["unchanged"] is False
    assert stored.result["source_checksum"] == SOURCE_SHA
    assert stored.result["source_locator"] == SOURCE_URL
    assert stored.result["source_key"] == "mgi"
    assert stored.result["warning_count"] == 0
    response = integration_api_client.get(f"/admin/jobs/{job_id}")
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "succeeded"
    assert response.json()["result"] == stored.result
    assert response.json()["result"]["warnings"] == []
    assert fetchers.calls == ["mgi"]


def test_gzip_compressed_source_publishes_the_same_catalog(
    unit_of_work_factory: UnitOfWorkFactory,
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
) -> None:
    """A gzip-compressed GPI file is expanded and published like a plain one."""
    job_id = _create_job(unit_of_work_factory)

    runner_for(_fetchers(gzip.compress(SOURCE_BYTES))).run(job_id)

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.progress["source_record_count"] == 1
    assert stored.result is not None
    assert stored.result["source_checksum"] != SOURCE_SHA


@pytest.mark.parametrize(
    "failure_code", ["timeout", "header", "row_validation", "catalog_collision"]
)
def test_entity_refresh_domain_failure_is_terminal_and_cleans_staging(
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
    integration_api_client: TestClient,
    failure_code: str,
) -> None:
    """Expected refresh failures fail the job with a generic message and a code.

    The failures are an unreachable source, an empty download, a row containing a
    NUL character, and an identifier that another source already supplies. Staged
    data is removed, the active catalogs are kept, and the logs omit source details
    such as identifiers, row content, and checksums.
    """
    log_records = _record_error_logs(monkeypatch)
    if failure_code == "catalog_collision":
        seed_active_subjects("MGI:1")
    else:
        runner_for(_fetchers()).run(_create_job(unit_of_work_factory))
    memberships_before = _memberships(session_factory)
    content = SOURCE_BYTES
    fetched: bytes | Exception = content
    if failure_code == "timeout":
        fetched = SourceError(RefreshFailureCode.TIMEOUT)
    elif failure_code == "header":
        fetched = content = b""
    elif failure_code == "row_validation":
        fetched = content = SOURCE_TEXT.replace("Protein", "Pro\x00tein").encode()
    job_id = _create_job(unit_of_work_factory)

    runner_for(_fetchers(fetched)).run(job_id)

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.FAILED
    assert stored.error == FAILED
    assert stored.progress["failure_code"] == failure_code
    assert stored.progress["phase"] == "failed"
    response = integration_api_client.get(f"/admin/jobs/{job_id}")
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["progress"]["failure_code"] == failure_code
    assert response.json()["error"] == FAILED
    assert "MGI:1" not in response.text
    audits = _audit_events(session_factory, AuditAction.JOB_FAILED, job_id)
    assert len(audits) == 1
    assert audits[0].details["failure_code"] == failure_code
    assert _staging_count(session_factory, job_id) == 0
    assert _memberships(session_factory) == memberships_before
    logs = "\n".join(log_records)
    assert str(job_id) in logs
    assert "MGI:1" not in logs
    assert "Gene1" not in logs
    assert sha256(content).hexdigest() not in logs
    assert "example.org" not in logs


def test_invalid_rows_are_reported_on_the_failed_job(
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    session_factory: sessionmaker[Session],
    integration_api_client: TestClient,
) -> None:
    """A file with invalid rows fails, and the job shows which rows and why.

    The job's progress lists every invalid row with its line number and reason,
    the failure audit event lists the first 10, the log names the first 5 rows
    without their rejected values, and the previous catalog stays active.
    """
    log_records = _record_error_logs(monkeypatch)
    runner_for(_fetchers()).run(_create_job(unit_of_work_factory))
    memberships_before = _memberships(session_factory)
    bad_rows = (
        "MGI:99\tA raw rejected symbol\tProtein\t\tSO:0001217\tNCBITaxon:9606\t\t"
        "MGI:99\t\t\t\n"
    ) + "".join(f"MGI:{index}\tshort\n" for index in range(100, 111))
    job_id = _create_job(unit_of_work_factory)

    runner_for(_fetchers((SOURCE_TEXT + bad_rows).encode())).run(job_id)

    response = integration_api_client.get(f"/admin/jobs/{job_id}")
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == FAILED
    assert body["progress"]["failure_code"] == "row_validation"
    details = body["progress"]["failure_details"]
    assert details["issue_count"] == 12
    assert [issue["line_number"] for issue in details["issues"]] == list(range(5, 17))
    [field] = details["issues"][0]["fields"]
    assert (field["field"], field["value"]) == (
        "db_object_symbol",
        "A raw rejected symbol",
    )
    assert details["issues"][1] == {
        "line_number": 6,
        "category": "field-count",
        "message": "expected 11 fields, found 2",
        "fields": [],
    }
    [audit] = _audit_events(session_factory, AuditAction.JOB_FAILED, job_id)
    assert audit.details["failure_code"] == "row_validation"
    audited = cast(dict[str, Any], audit.details["failure_details"])
    assert audited["issue_count"] == 12
    assert audited["issues"] == details["issues"][:10]
    assert _memberships(session_factory) == memberships_before
    logs = "\n".join(log_records)
    assert "issue_count=12" in logs
    assert "line 5 validation: fields db_object_symbol" in logs
    assert "line 6 field-count: expected 11 fields, found 2" in logs
    assert "line 10 " not in logs
    assert "A raw rejected symbol" not in logs


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"\x1f\x8bnot-gzip", "invalid_gzip"),
        (b"\xff\xfe", "invalid_utf8"),
        (gzip.compress(b"\xff\xfe"), "invalid_utf8"),
    ],
)
def test_undecodable_download_fails_with_its_code(
    unit_of_work_factory: UnitOfWorkFactory,
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    content: bytes,
    code: str,
) -> None:
    """Broken gzip or invalid UTF-8 fails the job without staging anything."""
    job_id = _create_job(unit_of_work_factory)

    runner_for(_fetchers(content)).run(job_id)

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.FAILED
    assert stored.error == FAILED
    assert stored.progress == {"phase": "failed", "failure_code": code}


def test_entity_refresh_cleanup_failure_rolls_back_terminal_state_until_redelivery(
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    session_factory: sessionmaker[Session],
    seed_active_subjects: Callable[..., None],
) -> None:
    """A failed cleanup of staging data also rolls back the job's failure record.

    The job stays running and the error propagates so the task retries. When it
    runs again, the job is marked failed and its staging data is removed
    together. Running the finished job once more changes nothing and does not
    contact the source.
    """
    seed_active_subjects("MGI:1")
    job_id = _create_job(unit_of_work_factory)
    fetchers = _fetchers()
    runner = runner_for(fetchers)
    original_cleanup = EntityRepository.cleanup_terminal_staging
    cleanup_failures = 1

    def fail_cleanup_once(repository: EntityRepository, received_job_id: UUID) -> None:
        nonlocal cleanup_failures
        if cleanup_failures:
            cleanup_failures -= 1
            raise RuntimeError("cleanup storage unavailable")
        original_cleanup(repository, received_job_id)

    monkeypatch.setattr(EntityRepository, "cleanup_terminal_staging", fail_cleanup_once)

    with pytest.raises(RuntimeError, match="cleanup storage unavailable"):
        runner.run(job_id)

    after_rollback = _read_job(jobs, job_id)
    assert after_rollback.status == JobStatus.RUNNING
    assert "failure_code" not in after_rollback.progress
    assert _staging_count(session_factory, job_id) == 1
    assert _audit_events(session_factory, AuditAction.JOB_FAILED, job_id) == []

    runner.run(job_id)

    terminal = _read_job(jobs, job_id)
    assert terminal.status == JobStatus.FAILED
    assert terminal.error == FAILED
    assert terminal.progress["failure_code"] == "catalog_collision"
    assert _staging_count(session_factory, job_id) == 0
    assert len(_audit_events(session_factory, AuditAction.JOB_FAILED, job_id)) == 1
    assert fetchers.calls == ["mgi"]

    runner.run(job_id)

    assert _read_job(jobs, job_id) == terminal
    assert len(_audit_events(session_factory, AuditAction.JOB_FAILED, job_id)) == 1
    assert fetchers.calls == ["mgi"]


def test_entity_refresh_redelivery_publishes_committed_staging_without_source_access(
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    session_factory: sessionmaker[Session],
) -> None:
    """After a publication failure, rerunning the job publishes the staged catalog.

    The interrupted job stays running with its staged data, and the rerun does not
    contact the source again.
    """
    job_id = _create_job(unit_of_work_factory)
    fetchers = _fetchers()
    runner = runner_for(fetchers)

    def outage(*args: object, **kwargs: object) -> None:
        raise RuntimeError("publication unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(EntityRepository, "publish", outage)
        with pytest.raises(RuntimeError, match="publication unavailable"):
            runner.run(job_id)
    interrupted = _read_job(jobs, job_id)
    assert interrupted.status == JobStatus.RUNNING
    assert interrupted.progress["phase"] == "publishing"
    with session_factory() as session:
        snapshot_id = session.scalar(select(EntityCatalogSnapshotRecord.snapshot_id))
        assert snapshot_id is not None
        assert (
            session.scalar(select(func.count()).select_from(EntityStagingRecord)) == 1
        )
    fetchers.contents["mgi"] = SourceError(RefreshFailureCode.HTTP_STATUS)

    runner.run(job_id)

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.result is not None
    assert stored.result["snapshot_id"] == str(snapshot_id)
    assert fetchers.calls == ["mgi"]
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(EntityStagingRecord)) == 0
        )
        assert list(session.scalars(select(EntityMembershipRecord.db_object_id))) == [
            "MGI:1"
        ]


@pytest.mark.parametrize("changed", ["source_key", "missing_row", "entity"])
def test_entity_refresh_redelivery_rejects_incompatible_or_incomplete_staging_before_fetch(
    unit_of_work_factory: UnitOfWorkFactory,
    jobs: JobService,
    entity_service: EntityRefreshService,
    runner_for: Callable[..., RefreshRunner],
    session_factory: sessionmaker[Session],
    changed: str,
) -> None:
    """Rerunning a job whose staged data no longer matches the job fails the job.

    The job fails with `candidate_conflict` without fetching the source, and the
    previous catalog stays active. Mismatches include staging for another source,
    a missing row, and an altered row.
    """
    fetchers = _fetchers()
    previous = _create_job(unit_of_work_factory)
    runner_for(fetchers).run(previous)
    job_id = _create_job(unit_of_work_factory)
    # Different content, so the refresh is not recognized as unchanged.
    document = _fetchers(SOURCE_TEXT.replace("Gene1", "Gene2").encode()).fetch(
        "mgi", TEST_SOURCES.source(RefreshKindName.ENTITY, "mgi")
    )
    stage_without_publishing(
        entity_service,
        jobs.find(job_id),
        document,
        (EntityRepository, "publish"),
    )
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
    fetchers = _fetchers(SourceError(RefreshFailureCode.HTTP_STATUS))

    runner_for(fetchers).run(job_id)

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.FAILED
    assert stored.progress["failure_code"] == "candidate_conflict"
    assert fetchers.calls == []
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


def test_unchanged_source_reports_the_active_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
) -> None:
    """A source matching the active catalog by locator and content completes unchanged.

    The job stores a result naming the active snapshot.
    """
    runner = runner_for(_fetchers())
    first = _create_job(unit_of_work_factory)
    runner.run(first)
    first_result = _read_job(jobs, first).result
    assert first_result is not None
    second = _create_job(unit_of_work_factory)

    runner.run(second)

    stored = _read_job(jobs, second)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.result == {
        "source_key": "mgi",
        "snapshot_id": first_result["snapshot_id"],
        "source_checksum": SOURCE_SHA,
        "unchanged": True,
    }
    assert stored.progress == {"phase": "completed", "unchanged": True}


@pytest.mark.parametrize("changed", ["url", "content"])
def test_changed_url_or_content_publishes_new_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
    jobs: JobService,
    runner_for: Callable[..., RefreshRunner],
    session_factory: sessionmaker[Session],
    changed: str,
) -> None:
    """A refresh with a changed URL or changed content publishes a new snapshot.

    Moving a source to a new URL publishes even when the content is identical, and
    new content at the same URL is never reported as unchanged.
    """
    runner_for(_fetchers()).run(_create_job(unit_of_work_factory))
    if changed == "url":
        expected_url, text = "https://example.org/moved/entities.gpi", SOURCE_TEXT
    else:
        expected_url, text = SOURCE_URL, SOURCE_TEXT.replace("Gene1", "Gene1b")
    sources = sources_with_entities({"mgi": {"type": "https", "url": expected_url}})
    job_id = _create_job(unit_of_work_factory)

    runner_for(_fetchers(text.encode()), sources=sources).run(job_id)

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.result is not None and stored.result["unchanged"] is False
    assert stored.result["source_locator"] == expected_url
    assert stored.result["source_checksum"] == sha256(text.encode()).hexdigest()
    assert stored.result["retained_count"] == 1
    published = _audit_events(session_factory, AuditAction.ENTITY_REFRESHED)
    assert len(published) == 2
    with session_factory() as session:
        active = session.scalar(
            select(EntityCatalogSnapshotRecord).where(
                EntityCatalogSnapshotRecord.active.is_(True)
            )
        )
        assert active is not None and active.job_id == job_id


def test_matching_catalog_of_another_source_does_not_make_refresh_unchanged(
    jobs: JobService,
    entity_service: EntityRefreshService,
    unit_of_work_factory: UnitOfWorkFactory,
    runner_for: Callable[..., RefreshRunner],
    session_factory: sessionmaker[Session],
) -> None:
    """Only the same source's active catalog can make a refresh unchanged.

    Another source's active catalog with the same locator and checksum is
    ignored, and the refresh publishes this source's catalog.
    """
    other_job = start_job(unit_of_work_factory, JobType.ENTITY_REFRESH, "rgd")
    mgi_document = _fetchers().fetch(
        "mgi", TEST_SOURCES.source(RefreshKindName.ENTITY, "mgi")
    )
    # The `rgd` catalog claims the `mgi` locator and checksum.
    entity_service.apply(
        other_job,
        replace(
            mgi_document,
            source_key="rgd",
            content=SOURCE_TEXT.replace("MGI:1", "RGD:1").encode(),
        ),
        ignore_progress,
    )
    job_id = _create_job(unit_of_work_factory)

    runner_for(_fetchers()).run(job_id)

    stored = _read_job(jobs, job_id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.result is not None and stored.result["unchanged"] is False
    assert stored.result["source_key"] == "mgi"
    assert stored.result["added_count"] == 1
    assert _memberships(session_factory) == [("MGI:1", "mgi"), ("RGD:1", "rgd")]
