"""Verify the shared refresh job lifecycle for every refresh kind."""

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from refresh_helpers import (
    TEST_SOURCES,
    FakeFetchers,
    build_runner,
    ignore_progress,
    start_job,
)
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.ontology import OntologyKey
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    RefreshKindName,
)
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    bind_try_lock,
    try_advisory_lock,
)
from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    JobRecord,
    OntologyMetadataRecord,
)
from standard_annotation_backend.persistence.repositories import OntologyRepository
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh import runner as runner_module
from standard_annotation_backend.refresh.fetchers import SourceError
from standard_annotation_backend.refresh.runner import RefreshRunner
from standard_annotation_backend.services import (
    ontology_refresh_service as ontology_module,
)
from standard_annotation_backend.services.authorization_refresh_service import (
    AuthorizationRefreshService,
)
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import JobService
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshBusyError,
    OntologyRefreshService,
)
from standard_annotation_backend.workers.runtime import create_refresh_runner

FIXTURES = Path(__file__).parents[1] / "fixtures"
USERS_YAML = b"""
- accounts: {github: curator}
  authorizations:
    sab: [{role: edit, scope: group, group: MGI}]
"""
INVALID_USERS_YAML = b"""
- accounts: {github: curator}
  authorizations:
    sab: [{role: owner, scope: group, group: MGI}]
"""


@dataclass(frozen=True, slots=True)
class KindCase:
    """Describe how one refresh kind is exercised by the shared lifecycle tests.

    Attributes:
        job_type: Job type of the kind's refresh jobs.
        source_key: A key configured in `TEST_SOURCES`.
        content: A valid source document.
        invalid_content: A document the kind rejects.
        invalid_code: Failure code recorded for `invalid_content`.
        applied_action: Audit action recorded once per applied document.
        public_error: Error stored on failed jobs.
        recovery_refetches: Whether recovery after a crash fetches again.
    """

    job_type: JobType
    source_key: str
    content: bytes
    invalid_content: bytes
    invalid_code: RefreshFailureCode
    applied_action: AuditAction
    public_error: str
    recovery_refetches: bool


CASES = {
    "entity": KindCase(
        job_type=JobType.ENTITY_REFRESH,
        source_key="mgi",
        content=(FIXTURES / "gpi" / "minimal.gpi").read_bytes(),
        invalid_content=(FIXTURES / "gpi" / "invalid-row.gpi").read_bytes(),
        invalid_code=RefreshFailureCode.ROW_VALIDATION,
        applied_action=AuditAction.ENTITY_REFRESHED,
        public_error="Entity refresh failed",
        recovery_refetches=False,
    ),
    "ontology": KindCase(
        job_type=JobType.ONTOLOGY_REFRESH,
        source_key="go",
        content=(FIXTURES / "ontology" / "minimal-go.obo").read_bytes(),
        invalid_content=(FIXTURES / "ontology" / "invalid-reference.obo").read_bytes(),
        invalid_code=RefreshFailureCode.INVALID_DOCUMENT,
        applied_action=AuditAction.ONTOLOGY_REFRESHED,
        public_error="Ontology refresh failed",
        recovery_refetches=False,
    ),
    "authorization": KindCase(
        job_type=JobType.AUTHORIZATION_REFRESH,
        source_key="go-site",
        content=USERS_YAML,
        invalid_content=INVALID_USERS_YAML,
        invalid_code=RefreshFailureCode.INVALID_DOCUMENT,
        applied_action=AuditAction.AUTHORIZATION_REFRESHED,
        public_error="Authorization refresh failed",
        recovery_refetches=True,
    ),
}


@pytest.fixture(params=sorted(CASES))
def case(request: pytest.FixtureRequest) -> KindCase:
    """Run each test once per refresh kind."""
    return CASES[request.param]


def _job(factory: UnitOfWorkFactory, case: KindCase, **parameters: object) -> UUID:
    return (
        JobService(factory)
        .create(
            job_type=case.job_type,
            requested_by="curator",
            parameters=parameters or {"source_key": case.source_key},
        )
        .job_id
    )


def _read(factory: UnitOfWorkFactory, job_id: UUID) -> JobRecord:
    with factory() as uow:
        record = uow.jobs.get(job_id)
        assert record is not None
        uow.jobs.session.expunge(record)
        return record


def _count(session_factory: sessionmaker[Session], action: AuditAction) -> int:
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(AuditEventRecord)
                .where(AuditEventRecord.action == action)
            )
            or 0
        )


def test_refresh_applies_once_then_reports_unchanged(
    case: KindCase,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A new document is applied; refreshing the same document applies nothing."""
    fetchers = FakeFetchers({case.source_key: case.content})
    runner = build_runner(database_engine, unit_of_work_factory, fetchers)
    first, second = _job(unit_of_work_factory, case), _job(unit_of_work_factory, case)

    runner.run(first)
    runner.run(second)

    for job_id in (first, second):
        record = _read(unit_of_work_factory, job_id)
        assert record.status == JobStatus.SUCCEEDED
        assert record.progress["phase"] == "completed"
    first_record = _read(unit_of_work_factory, first)
    second_record = _read(unit_of_work_factory, second)
    # Every kind reports "nothing applied" the same way, in progress and result.
    assert first_record.progress["unchanged"] is False
    assert first_record.result is not None
    assert first_record.result["unchanged"] is False
    assert second_record.progress["unchanged"] is True
    assert second_record.result is not None
    assert second_record.result["unchanged"] is True
    for record in (first_record, second_record):
        assert record.result is not None and "applied" not in record.result
    assert fetchers.calls == [case.source_key, case.source_key]
    assert _count(session_factory, case.applied_action) == 1


def test_finished_job_is_not_run_again(
    case: KindCase,
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """A redelivered message for a finished job fetches nothing."""
    fetchers = FakeFetchers({case.source_key: case.content})
    runner = build_runner(database_engine, unit_of_work_factory, fetchers)
    job_id = _job(unit_of_work_factory, case)

    runner.run(job_id)
    runner.run(job_id)

    assert fetchers.calls == [case.source_key]


@pytest.mark.parametrize(
    "code",
    [
        RefreshFailureCode.TIMEOUT,
        RefreshFailureCode.HTTP_STATUS,
        RefreshFailureCode.SOURCE_ERROR,
    ],
)
def test_source_failure_is_terminal_with_its_code(
    case: KindCase,
    code: RefreshFailureCode,
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """Retrieval failures fail the job with the retrieval code and a kind message."""
    runner = build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({case.source_key: SourceError(code)}),
    )
    job_id = _job(unit_of_work_factory, case)

    runner.run(job_id)

    record = _read(unit_of_work_factory, job_id)
    assert record.status == JobStatus.FAILED
    assert record.error == case.public_error
    assert record.progress == {"phase": "failed", "failure_code": code.value}


def test_invalid_document_is_terminal_with_the_kind_code(
    case: KindCase,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A document the kind rejects fails the job and applies nothing."""
    runner = build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({case.source_key: case.invalid_content}),
    )
    job_id = _job(unit_of_work_factory, case)

    runner.run(job_id)

    record = _read(unit_of_work_factory, job_id)
    assert record.status == JobStatus.FAILED
    assert record.progress["failure_code"] == case.invalid_code.value
    assert "failure_details" in record.progress
    assert _count(session_factory, case.applied_action) == 0


def test_unconfigured_source_fails_without_fetching(
    case: KindCase,
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """A job for a source removed from the sources file fails as unknown."""
    fetchers = FakeFetchers({})
    runner = build_runner(database_engine, unit_of_work_factory, fetchers)
    job_id = _job(unit_of_work_factory, case, source_key="removed")

    runner.run(job_id)

    record = _read(unit_of_work_factory, job_id)
    assert record.status == JobStatus.FAILED
    assert record.error == case.public_error
    assert record.progress["failure_code"] == "unknown_source"
    assert fetchers.calls == []


@pytest.mark.parametrize(
    "parameters",
    [
        {"source_key": "Not A Key"},
        {"source_key": "go\n"},
        {"source_key": 7},
        {"source_key": "go", "other": 1},
        {"ontology": "go"},
        {},
    ],
)
def test_invalid_parameters_fail_without_fetching(
    case: KindCase,
    parameters: dict[str, object],
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """Parameters other than exactly one valid `source_key` fail the job."""
    fetchers = FakeFetchers({})
    runner = build_runner(database_engine, unit_of_work_factory, fetchers)
    job_id = (
        JobService(unit_of_work_factory)
        .create(job_type=case.job_type, requested_by="curator", parameters=parameters)
        .job_id
    )

    runner.run(job_id)

    record = _read(unit_of_work_factory, job_id)
    assert record.status == JobStatus.FAILED
    assert record.error == case.public_error
    assert record.progress["failure_code"] == "invalid_parameters"
    assert fetchers.calls == []


def test_infrastructure_error_leaves_job_running_for_retry(
    case: KindCase,
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """An unexpected error propagates so Celery retries; a retry then succeeds."""
    job_id = _job(unit_of_work_factory, case)
    broken = build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({case.source_key: ConnectionResetError("database lost")}),
    )

    with pytest.raises(ConnectionResetError):
        broken.run(job_id)

    assert _read(unit_of_work_factory, job_id).status == JobStatus.RUNNING
    build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({case.source_key: case.content}),
    ).run(job_id)
    assert _read(unit_of_work_factory, job_id).status == JobStatus.SUCCEEDED


def test_crash_after_apply_recovers_the_committed_result(
    case: KindCase,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the worker stops after applying, redelivery finishes without reapplying."""
    fetchers = FakeFetchers({case.source_key: case.content})
    runner = build_runner(database_engine, unit_of_work_factory, fetchers)
    job_id = _job(unit_of_work_factory, case)
    original_succeed = JobService.succeed

    def crash(*_args: object, **_kwargs: object) -> None:
        raise ConnectionResetError("worker lost")

    monkeypatch.setattr(JobService, "succeed", crash)
    with pytest.raises(ConnectionResetError):
        runner.run(job_id)
    monkeypatch.setattr(JobService, "succeed", original_succeed)

    runner.run(job_id)

    recovered = _read(unit_of_work_factory, job_id)
    assert recovered.status == JobStatus.SUCCEEDED
    # Kinds that recover the committed result report it as applied. A kind
    # that fetches again finds its own committed document already active.
    assert recovered.result is not None
    assert recovered.result["unchanged"] is case.recovery_refetches
    assert recovered.progress["unchanged"] is case.recovery_refetches
    assert _count(session_factory, case.applied_action) == 1
    expected_calls = 2 if case.recovery_refetches else 1
    assert fetchers.calls == [case.source_key] * expected_calls


def test_ontology_job_for_a_non_ontology_key_fails_as_unknown_source(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """A key that matches the pattern but is not an ontology fails; it never retries."""
    fetchers = FakeFetchers({})
    runner = build_runner(database_engine, unit_of_work_factory, fetchers)
    job_id = (
        JobService(unit_of_work_factory)
        .create(
            job_type=JobType.ONTOLOGY_REFRESH,
            requested_by="curator",
            parameters={"source_key": "chebi"},
        )
        .job_id
    )

    runner.run(job_id)

    record = _read(unit_of_work_factory, job_id)
    assert record.status == JobStatus.FAILED
    assert record.progress["failure_code"] == "unknown_source"
    assert fetchers.calls == []


def test_ontology_refresh_waits_for_the_ontology_lock_by_retrying(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While another process holds the GO lock, the run raises so Celery retries."""
    # Alembic's logging setup, run by earlier integration tests, disables loggers
    # that already exist; re-enable this one so `caplog` can see its records.
    monkeypatch.setattr(runner_module.logger, "disabled", False)
    runner = build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({"go": CASES["ontology"].content}),
    )
    job_id = _job(unit_of_work_factory, CASES["ontology"])

    with try_advisory_lock(
        database_engine, LockNamespace.ONTOLOGY, OntologyKey.GO.value
    ) as acquired:
        assert acquired
        with (
            caplog.at_level(
                logging.INFO, logger="standard_annotation_backend.refresh.runner"
            ),
            pytest.raises(OntologyRefreshBusyError),
        ):
            runner.run(job_id)

    waiting = [r for r in caplog.records if "Refresh job waiting" in r.getMessage()]
    assert [r.levelno for r in waiting] == [logging.INFO]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert _read(unit_of_work_factory, job_id).status == JobStatus.RUNNING
    runner.run(job_id)
    assert _read(unit_of_work_factory, job_id).status == JobStatus.SUCCEEDED


def test_finished_ontology_jobs_schedule_pruning_every_time(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """Pruning is scheduled after success, after failure, and on redelivery."""
    scheduled: list[str] = []
    ontology = CASES["ontology"]
    succeeded = _job(unit_of_work_factory, ontology)
    build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({"go": ontology.content}),
        enqueue_prune=scheduled.append,
    ).run(succeeded)
    failed = _job(unit_of_work_factory, ontology)
    failing = build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({"go": ontology.invalid_content}),
        enqueue_prune=scheduled.append,
    )
    failing.run(failed)
    failing.run(failed)

    assert scheduled == ["go", "go", "go"]


def test_redelivered_authorization_job_reuses_the_committed_refresh(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the replacement commits leads to `unchanged: true`, not a reapply."""
    case = CASES["authorization"]
    runner = build_runner(
        database_engine, unit_of_work_factory, FakeFetchers({"go-site": case.content})
    )
    job_id = _job(unit_of_work_factory, case)
    original = JobService.succeed

    def crash(*_args: object, **_kwargs: object) -> None:
        raise ConnectionResetError("worker lost")

    monkeypatch.setattr(JobService, "succeed", crash)
    with pytest.raises(ConnectionResetError):
        runner.run(job_id)
    monkeypatch.setattr(JobService, "succeed", original)
    runner.run(job_id)

    record = _read(unit_of_work_factory, job_id)
    assert record.status == JobStatus.SUCCEEDED
    assert record.result is not None and record.result["unchanged"] is True
    assert record.progress["unchanged"] is True
    assert record.parameters == {"source_key": "go-site"}
    assert _count(session_factory, AuditAction.AUTHORIZATION_REFRESHED) == 1


def test_invalid_users_yaml_reports_issues_without_values(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """Validation issues are recorded with locations and types, at most 100."""
    case = CASES["authorization"]
    runner = build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({"go-site": case.invalid_content}),
    )
    job_id = _job(unit_of_work_factory, case)

    runner.run(job_id)

    details = cast(
        dict[str, Any], _read(unit_of_work_factory, job_id).progress["failure_details"]
    )
    assert details["issue_count"] >= 1
    assert {"location", "message", "type"} == set(details["issues"][0])
    assert "owner" not in str(details)


def _go_versions(content: bytes, count: int) -> list[bytes]:
    """Return `count` valid GO documents that differ only in `data-version`."""
    return [
        content.replace(
            b"data-version: releases/2026-09-28",
            f"data-version: releases/2026-09-{number:02d}".encode(),
        )
        for number in range(count)
    ]


def _pruned_snapshot_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(OntologyMetadataRecord)
                .where(OntologyMetadataRecord.bulk_data_pruned_at.is_not(None))
            )
            or 0
        )


def test_in_process_pruning_deletes_old_snapshot_data(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """Without `enqueue_prune`, finished ontology jobs prune old snapshots themselves."""
    case = CASES["ontology"]
    # Three distinct documents leave one active snapshot, one retained
    # predecessor, and one snapshot that is eligible for pruning.
    for content in _go_versions(case.content, 3):
        runner = build_runner(
            database_engine,
            unit_of_work_factory,
            FakeFetchers({"go": content}),
            enqueue_prune=None,
        )
        job_id = _job(unit_of_work_factory, case)
        runner.run(job_id)
        assert _read(unit_of_work_factory, job_id).status == JobStatus.SUCCEEDED

    assert _pruned_snapshot_count(session_factory) == 1


def test_in_process_pruning_is_skipped_while_the_ontology_lock_is_held(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy ontology is left for the next finished job and does not raise."""
    pruned: list[object] = []
    monkeypatch.setattr(
        OntologyRepository,
        "prune_candidates",
        lambda *a, **k: pruned.append((a, k)),
    )
    components = create_refresh_runner(
        engine=database_engine,
        unit_of_work_factory=unit_of_work_factory,
        sources=TEST_SOURCES,
        fetchers=FakeFetchers({}),
        enqueue_prune=None,
    )

    with try_advisory_lock(
        database_engine, LockNamespace.ONTOLOGY, OntologyKey.GO.value
    ) as acquired:
        assert acquired
        components.ontology.after_terminal("go")

    assert pruned == []
    components.ontology.after_terminal("go")
    assert len(pruned) == 1


def test_in_process_pruning_failure_is_logged_and_the_job_stays_succeeded(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception while pruning is logged by type only and does not fail the run."""
    # Alembic's logging setup, run by earlier integration tests, disables loggers
    # that already exist; re-enable this one so `caplog` can see its records.
    monkeypatch.setattr(ontology_module.logger, "disabled", False)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("secret connection detail")

    monkeypatch.setattr(OntologyRepository, "prune_candidates", broken)
    case = CASES["ontology"]
    runner = build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({"go": case.content}),
        enqueue_prune=None,
    )
    job_id = _job(unit_of_work_factory, case)

    with caplog.at_level("ERROR"):
        runner.run(job_id)

    assert _read(unit_of_work_factory, job_id).status == JobStatus.SUCCEEDED
    assert "RuntimeError" in caplog.text
    assert "go" in caplog.text
    assert "secret connection detail" not in caplog.text


def test_job_type_no_kind_claims_fails_generically_without_fetching(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """A retirement job delivered to the refresh task fails once, generically."""
    fetchers = FakeFetchers({})
    runner = build_runner(database_engine, unit_of_work_factory, fetchers)
    job_id = (
        JobService(unit_of_work_factory)
        .create(
            job_type=JobType.ENTITY_RETIREMENT,
            requested_by="curator",
            parameters={"source_key": "mgi"},
        )
        .job_id
    )

    runner.run(job_id)

    record = _read(unit_of_work_factory, job_id)
    assert record.status == JobStatus.FAILED
    assert record.error == "Refresh failed"
    assert record.progress == {"phase": "failed", "failure_code": "invalid_parameters"}
    assert fetchers.calls == []


def test_document_activated_while_waiting_for_the_lock_reports_unchanged(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A job that loses the race to the same document applies nothing."""
    content = CASES["ontology"].content
    other = _job(unit_of_work_factory, CASES["ontology"])
    racing = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    other_runner = build_runner(
        database_engine, unit_of_work_factory, FakeFetchers({"go": content})
    )
    bound = bind_try_lock(database_engine)

    def try_lock_after_the_other_job(
        namespace: LockNamespace, value: str | UUID | None = None
    ) -> AbstractContextManager[bool]:
        # The other job runs to completion between this job's pre-lock check
        # and its lock acquisition.
        if namespace is LockNamespace.ONTOLOGY:
            other_runner.run(other)
        return bound(namespace, value)

    service = OntologyRefreshService(
        unit_of_work_factory, try_lock_after_the_other_job, None
    )
    document = FakeFetchers({"go": content}).fetch(
        "go", TEST_SOURCES.source(RefreshKindName.ONTOLOGY, "go")
    )

    outcome = service.apply(racing, document, ignore_progress)

    assert outcome.unchanged is True
    assert _count(session_factory, AuditAction.ONTOLOGY_REFRESHED) == 1
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(OntologyMetadataRecord))
            == 1
        )


def test_two_kinds_claiming_one_job_type_are_rejected(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """Each job type is run by exactly one kind, so ambiguity fails at startup."""
    authorization = AuthorizationRefreshService(unit_of_work_factory)

    with pytest.raises(ValueError, match="more than one refresh kind"):
        RefreshRunner(
            try_lock=bind_try_lock(database_engine),
            jobs=JobService(unit_of_work_factory),
            fetchers=FakeFetchers({}),
            sources=TEST_SOURCES,
            kinds=(authorization, authorization),
            retirer=EntityRefreshService(unit_of_work_factory, TEST_SOURCES),
        )
