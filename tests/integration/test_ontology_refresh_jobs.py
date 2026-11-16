"""Verify ontology refresh jobs run by the Celery worker tasks."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx2
import pytest
from seeding import create_job

from standard_annotation_backend.config import get_settings
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.workers import tasks

SHA = "a" * 40
WORKER_SOURCES = """\
authorization:
  type: github
  repository: geneontology/go-site
  ref: master
  path: metadata/users.yaml
rules:
  type: github
  repository: geneontology/go-site
  ref: master
ontologies:
  go:
    type: github
    repository: geneontology/go-ontology
    ref: master
    path: src/ontology/go-edit.obo
    document_ontology_id: go
    prefixes: [GO]
    closure_predicates: [rdfs:subClassOf, BFO:0000050]
    required_subsets: [gocheck_do_not_annotate, gocheck_obsoletion_candidate]
"""
OBO = (
    Path(__file__).parents[1] / "fixtures" / "ontology" / "minimal-go.obo"
).read_bytes()


@pytest.fixture
def ontology_worker_environment(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Point worker-created engines at the guarded disposable test database.

    Workers read a sources file whose only ontology is GO from GitHub, so the fake
    GitHub responses below answer every request, whatever `config/sources.yaml`
    configures.
    """
    sources_file = tmp_path / "sources.yaml"
    sources_file.write_text(WORKER_SOURCES, encoding="utf-8")
    monkeypatch.setenv("SABE_DATABASE_URL", os.environ["SABE_TEST_DATABASE_URL"])
    monkeypatch.setenv("SABE_SOURCES_FILE", str(sources_file))
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


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


def test_worker_reports_an_undefined_consider_target_as_a_warning(
    ontology_worker_environment: None,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dangling `consider` target produces a successful job with a warning."""
    content = b"""\
format-version: 1.4
ontology: go
subsetdef: gocheck_do_not_annotate "Term not to be used for direct annotation"
subsetdef: gocheck_obsoletion_candidate "Terms planned for obsoletion"

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
    if failure_code == "invalid_document":
        details = cast(dict[str, Any], job.progress["failure_details"])
        message = details["message"]
        assert isinstance(message, str)
        assert message.strip()
    else:
        assert "failure_details" not in job.progress


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
