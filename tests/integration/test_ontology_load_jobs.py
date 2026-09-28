"""Verify stored ontology-load worker state and recovery."""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
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
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.job_service import JobService
from standard_annotation_backend.services.ontology_load_service import (
    OntologyLoadService,
)
from standard_annotation_backend.workers.tasks import (
    WorkerTaskUnavailableError,
    run_ontology_load,
    schedule_ontology_load,
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


def _create_job(factory: UnitOfWorkFactory) -> UUID:
    return (
        JobService(factory)
        .create(
            job_type=JobType.ONTOLOGY_LOAD,
            requested_by="ontology-worker",
            parameters={"ontology": "go"},
        )
        .job_id
    )


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


def _source_responses(content: bytes = OBO) -> list[httpx2.Response]:
    return [
        httpx2.Response(200, json={"sha": SHA}),
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

    run_ontology_load.run(str(first_id))
    run_ontology_load.run(str(second_id))

    first = _load_job(unit_of_work_factory, first_id)
    second = _load_job(unit_of_work_factory, second_id)
    assert first.status == second.status == JobStatus.SUCCEEDED.value
    assert first.result is not None and first.result["applied"] is True
    assert second.result is not None and second.result["applied"] is False
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
                .where(AuditEventRecord.action == AuditAction.ONTOLOGY_LOADED.value)
            )
            == 1
        )


@pytest.mark.parametrize(
    "responses",
    [
        [httpx2.Response(503, content=b"upstream secret")],
        _source_responses(b"[not obo"),
    ],
)
def test_source_and_parse_failures_store_only_public_error(
    ontology_worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[httpx2.Response],
) -> None:
    """Deterministic retrieval and parsing failures do not expose diagnostics."""
    job_id = _create_job(unit_of_work_factory)
    _install_source(monkeypatch, responses.copy())

    run_ontology_load.run(str(job_id))

    job = _load_job(unit_of_work_factory, job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.error == "Ontology load failed"
    assert "secret" not in job.error


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
        OntologyLoadService,
        method_name,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("detail")),
    )

    with pytest.raises(WorkerTaskUnavailableError):
        run_ontology_load.run(str(job_id))

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
        uow.annotations.create(
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
        run_ontology_load.run(str(job_id))
    monkeypatch.setattr(JobService, finalization_method, original_finalization)

    run_ontology_load.run(str(job_id))

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
                .where(AuditEventRecord.action == AuditAction.ONTOLOGY_LOADED.value)
            )
            == 1
        )


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

    monkeypatch.setattr(run_ontology_load, "delay", fail_dispatch)

    schedule_ontology_load.run()

    assert len(dispatched) == 1
    job = _load_job(unit_of_work_factory, UUID(dispatched[0]))
    assert job.requested_by == "scheduler"
    assert job.parameters == {"ontology": "go"}
    assert job.status == JobStatus.FAILED.value
    assert job.error == "Ontology load could not be dispatched"
