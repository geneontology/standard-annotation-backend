"""Verify stored ontology refresh worker state and recovery."""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from seeding import create_job, insert_annotation
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.config import get_settings
from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.models import (
    AnnotationOrigin,
    AuditEventRecord,
    JobRecord,
    OntologyMetadataRecord,
)
from standard_annotation_backend.persistence.repositories import OntologyRepository
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.job_service import JobService
from standard_annotation_backend.workers import tasks
from standard_annotation_backend.workers.tasks import (
    WorkerTaskUnavailableError,
    prune_ontology_snapshots,
)

SHA = "a" * 40
OBO = (
    Path(__file__).parents[1] / "fixtures" / "ontology" / "minimal-go.obo"
).read_bytes()


@pytest.fixture
def ontology_worker_environment(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Point worker-created engines at the guarded disposable test database."""
    monkeypatch.setenv("SAB_DATABASE_URL", os.environ["SAB_TEST_DATABASE_URL"])
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def suppress_pruning_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ontology worker tests independent of a live Celery broker."""
    monkeypatch.setattr(prune_ontology_snapshots, "delay", lambda _key: None)


def _create_job(factory: UnitOfWorkFactory) -> UUID:
    return create_job(
        factory,
        job_type=JobType.ONTOLOGY_REFRESH,
        requested_by="ontology-worker",
        parameters={"source_key": "go"},
    ).job_id


def _load_job(factory: UnitOfWorkFactory, job_id: UUID) -> JobRecord:
    with factory() as uow:
        record = uow.jobs.get(job_id)
        assert record is not None
        uow.jobs.session.expunge(record)
        return record


def _install_source(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[httpx2.Response],
) -> list[httpx2.Request]:
    requests: list[httpx2.Request] = []

    def handle_request(
        _transport: httpx2.HTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
        requests.append(request)
        response = responses.pop(0)
        return httpx2.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
        )

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", handle_request)
    return requests


def _source_responses(
    content: bytes = OBO,
    *,
    revision: str = SHA,
) -> list[httpx2.Response]:
    return [
        httpx2.Response(200, json={"sha": revision}),
        httpx2.Response(200, content=content),
    ]


def test_worker_loads_ontology_then_skips_unchanged_source(
    ontology_worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed source activates once; the same source does not create a new version."""
    first_id = _create_job(unit_of_work_factory)
    second_id = _create_job(unit_of_work_factory)
    requests = _install_source(monkeypatch, _source_responses() + _source_responses())

    tasks.run_refresh.run(str(first_id))
    tasks.run_refresh.run(str(second_id))

    first = _load_job(unit_of_work_factory, first_id)
    second = _load_job(unit_of_work_factory, second_id)
    assert first.status == second.status == JobStatus.SUCCEEDED.value
    assert first.result is not None and first.result["unchanged"] is False
    assert second.result is not None and second.result["unchanged"] is True
    assert len(requests) == 4
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(OntologyMetadataRecord))
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEventRecord)
                .where(AuditEventRecord.action == AuditAction.ONTOLOGY_REFRESHED.value)
            )
            == 1
        )


@pytest.mark.parametrize(
    ("responses", "expected_status"),
    [
        (_source_responses(), JobStatus.SUCCEEDED),
        (_source_responses(b"[not obo"), JobStatus.FAILED),
    ],
)
def test_terminal_ontology_job_dispatches_pruning_after_status_commit(
    ontology_worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[httpx2.Response],
    expected_status: JobStatus,
) -> None:
    """Successful and failed refreshes dispatch pruning after storing terminal status."""
    job_id = _create_job(unit_of_work_factory)
    _install_source(monkeypatch, responses.copy())
    observed: list[tuple[str, str]] = []

    def observe_dispatch(key: str) -> None:
        observed.append((key, _load_job(unit_of_work_factory, job_id).status))

    monkeypatch.setattr(prune_ontology_snapshots, "delay", observe_dispatch)

    tasks.run_refresh.run(str(job_id))

    assert observed == [("go", expected_status.value)]


def test_terminal_redelivery_retries_pruning_dispatch_without_changing_result(
    ontology_worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch failure preserves job success, and redelivery dispatches pruning."""
    job_id = _create_job(unit_of_work_factory)
    _install_source(monkeypatch, _source_responses())

    def fail_dispatch(_key: str) -> None:
        raise RuntimeError("redis detail")

    monkeypatch.setattr(prune_ontology_snapshots, "delay", fail_dispatch)
    with pytest.raises(WorkerTaskUnavailableError):
        tasks.run_refresh.run(str(job_id))
    terminal = _load_job(unit_of_work_factory, job_id)
    assert terminal.status == JobStatus.SUCCEEDED.value
    assert terminal.result is not None
    original_result = dict(terminal.result)

    dispatched: list[str] = []
    monkeypatch.setattr(prune_ontology_snapshots, "delay", dispatched.append)
    tasks.run_refresh.run(str(job_id))

    recovered = _load_job(unit_of_work_factory, job_id)
    assert dispatched == ["go"]
    assert recovered.status == JobStatus.SUCCEEDED.value
    assert recovered.result == original_result


def test_pruning_task_retries_infrastructure_failure(
    ontology_worker_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pruning infrastructure failure requests a task retry."""
    monkeypatch.setattr(
        OntologyRepository,
        "prune_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database")),
    )

    with pytest.raises(WorkerTaskUnavailableError):
        prune_ontology_snapshots.run("go")


def test_pruning_task_ignores_an_unknown_ontology_key(
    ontology_worker_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key that is not a supported ontology is neither pruned nor retried."""
    pruned: list[object] = []
    monkeypatch.setattr(
        OntologyRepository,
        "prune_candidates",
        lambda *_args, **_kwargs: pruned.append(object()),
    )

    prune_ontology_snapshots.run("chebi")

    assert pruned == []


def test_worker_reports_an_undefined_consider_target_as_a_warning(
    ontology_worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dangling `consider` target produces a successful job with a warning."""
    content = b"""\
format-version: 1.4
ontology: go

[Term]
id: GO:0000001
is_obsolete: true
consider: GO:9999999
"""
    job_id = _create_job(unit_of_work_factory)
    _install_source(monkeypatch, _source_responses(content))

    tasks.run_refresh.run(str(job_id))

    job = _load_job(unit_of_work_factory, job_id)
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.progress["ontology_warning_count"] == 1
    assert job.warnings == ["Ontology refresh completed with 1 ontology warning"]
    assert job.result is not None
    assert job.result["ontology_warnings"] == [
        {
            "code": "undefined_consider_target",
            "term_id": "GO:0000001",
            "referenced_term_id": "GO:9999999",
        }
    ]


@pytest.mark.parametrize(
    ("responses", "failure_code"),
    [
        ([httpx2.Response(503, content=b"upstream secret")], "source_error"),
        (_source_responses(b"[not obo"), "invalid_document"),
    ],
)
def test_source_and_parse_failures_store_only_public_error(
    ontology_worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[httpx2.Response],
    failure_code: str,
) -> None:
    """Deterministic retrieval and parsing failures do not expose diagnostics.

    Each failure records its machine-readable code. The parse failure also
    records the parser's message in `failure_details`, which is not the public
    `error` text.
    """
    job_id = _create_job(unit_of_work_factory)
    _install_source(monkeypatch, responses.copy())

    tasks.run_refresh.run(str(job_id))

    job = _load_job(unit_of_work_factory, job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.error == "Ontology refresh failed"
    assert "secret" not in job.error
    assert job.progress["failure_code"] == failure_code
    assert "secret" not in str(job.progress)


@pytest.mark.parametrize("method_name", ["stage", "activate"])
def test_staging_and_activation_infrastructure_failures_request_retry(
    ontology_worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Unexpected persistence failures remain retryable rather than terminal."""
    job_id = _create_job(unit_of_work_factory)
    _install_source(monkeypatch, _source_responses())
    monkeypatch.setattr(
        OntologyRepository,
        method_name,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("detail")),
    )

    with pytest.raises(WorkerTaskUnavailableError):
        tasks.run_refresh.run(str(job_id))

    job = _load_job(unit_of_work_factory, job_id)
    assert job.status == JobStatus.RUNNING.value
    assert job.error is None


@pytest.mark.parametrize("finalization_method", ["update_progress", "succeed"])
def test_redelivery_after_activation_recovers_exact_result_without_reapplying(
    ontology_worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    finalization_method: str,
) -> None:
    """A finalization retry reuses the stored result without updating annotations again."""
    annotation_id = UUID("00000000-0000-0000-0000-000000000081")
    annotation = Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:RECOVERY",
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0000002",
            "references": ["PMID:recovery"],
            "evidence_type": "ECO:0000314",
            "annotation_date": "2026-09-28",
            "assigned_by": "GO_Central",
            "annotation_extensions": [
                {
                    "extension_relation": "BFO:0000050",
                    "extension_term": "GO:9999999",
                }
            ],
        }
    )
    with unit_of_work_factory() as uow:
        insert_annotation(
            uow.annotations.session,
            annotation=annotation,
            actor_id="creator",
            change_source="test",
            owning_group_id="group-1",
            record_origin=AnnotationOrigin.DIRECT,
            annotation_id=annotation_id,
        )
        uow.commit()
    job_id = _create_job(unit_of_work_factory)
    requests = _install_source(monkeypatch, _source_responses())
    original_finalization = getattr(JobService, finalization_method)

    monkeypatch.setattr(
        JobService,
        finalization_method,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database")),
    )
    with pytest.raises(WorkerTaskUnavailableError):
        tasks.run_refresh.run(str(job_id))
    monkeypatch.setattr(JobService, finalization_method, original_finalization)

    tasks.run_refresh.run(str(job_id))

    recovered = _load_job(unit_of_work_factory, job_id)
    assert recovered.status == JobStatus.SUCCEEDED.value
    assert recovered.result is not None
    assert recovered.result["annotation_scan_count"] == 1
    assert recovered.result["annotation_update_count"] == 1
    assert recovered.result["findings"] == [
        {
            "annotation_id": str(annotation_id),
            "code": "removed_term",
            "conflicting_annotation_ids": [],
            "field_paths": ["/annotation_extensions/0/extension_term"],
            "ontology": "go",
            "replacement_ids": [],
            "term_id": "GO:9999999",
        }
    ]
    assert len(requests) == 2
    with unit_of_work_factory() as uow:
        record = uow.annotations.get(annotation_id)
        assert record is not None
        assert record.current_version == 2
        assert record.ontology_class_id == "GO:0000001"
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(OntologyMetadataRecord))
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEventRecord)
                .where(AuditEventRecord.action == AuditAction.ONTOLOGY_REFRESHED.value)
            )
            == 1
        )


def test_redelivery_recovers_result_after_a_later_load_becomes_active(
    ontology_worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A superseded refresh still recovers its stored result on redelivery."""
    first_id = _create_job(unit_of_work_factory)
    second_id = _create_job(unit_of_work_factory)
    requests = _install_source(
        monkeypatch,
        _source_responses() + _source_responses(revision="b" * 40),
    )
    original_succeed = JobService.succeed
    monkeypatch.setattr(
        JobService,
        "succeed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database")),
    )

    with pytest.raises(WorkerTaskUnavailableError):
        tasks.run_refresh.run(str(first_id))
    monkeypatch.setattr(JobService, "succeed", original_succeed)

    with session_factory() as session:
        first_version = session.scalar(
            select(OntologyMetadataRecord).where(
                OntologyMetadataRecord.job_id == first_id
            )
        )
        assert first_version is not None
        assert first_version.active is True
        assert first_version.refresh_result is not None
        stored_result = dict(first_version.refresh_result)

    tasks.run_refresh.run(str(second_id))

    with session_factory() as session:
        first_version = session.scalar(
            select(OntologyMetadataRecord).where(
                OntologyMetadataRecord.job_id == first_id
            )
        )
        active_version = session.scalar(
            select(OntologyMetadataRecord).where(
                OntologyMetadataRecord.active.is_(True)
            )
        )
        assert first_version is not None and first_version.active is False
        assert active_version is not None and active_version.job_id == second_id

    tasks.run_refresh.run(str(first_id))

    recovered = _load_job(unit_of_work_factory, first_id)
    assert recovered.status == JobStatus.SUCCEEDED.value
    # The recovered job reports the result its snapshot stored when activated.
    assert recovered.result == {**stored_result, "unchanged": False}
    assert len(requests) == 4


def test_scheduler_persists_only_key_and_records_dispatch_failure(
    ontology_worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduler commits its job before a failed worker dispatch."""
    dispatched: list[str] = []

    def fail_dispatch(job_id: str) -> None:
        dispatched.append(job_id)
        raise RuntimeError("redis detail")

    monkeypatch.setattr(tasks.run_refresh, "delay", fail_dispatch)

    tasks.schedule_refresh.run("ontology")

    assert len(dispatched) == 1
    job = _load_job(unit_of_work_factory, UUID(dispatched[0]))
    assert job.requested_by == "scheduler"
    assert job.parameters == {"source_key": "go"}
    assert job.status == JobStatus.FAILED.value
    assert job.error == "Ontology refresh could not be dispatched"
