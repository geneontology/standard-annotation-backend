"""Verify the shared refresh job lifecycle for every refresh kind."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from annotation_refresh_helpers import gpad_bytes, gpad_row
from refresh_helpers import (
    TEST_SOURCES,
    FakeFetchers,
    OboTerm,
    build_runner,
    gpi_bytes,
    obo,
    rule_files,
)
from seeding import create_job, snapshot_count
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobFailureCode, JobStatus, JobType
from standard_annotation_backend.domain.ontology import (
    OntologyClosureRow,
    OntologySnapshot,
)
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.go_site.rule_files import GOREFS_PATH
from standard_annotation_backend.persistence.locks import bind_try_lock
from standard_annotation_backend.persistence.models import (
    AnnotationImportRecord,
    AuditEventRecord,
    AuthorizationRefreshRecord,
    Base,
    EntitySnapshotDetailRecord,
    JobRecord,
    OntologySnapshotDetailRecord,
    ReferenceSnapshotRecord,
    RulesSnapshotDetailRecord,
)
from standard_annotation_backend.persistence.repositories.snapshots import (
    SnapshotRepository,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh import runner as refresh_runner
from standard_annotation_backend.refresh.fetchers import SourceError
from standard_annotation_backend.refresh.runner import RefreshRunner
from standard_annotation_backend.services import ontology_refresh_service
from standard_annotation_backend.services.annotation_scan import queue_or_join
from standard_annotation_backend.services.authorization_refresh_service import (
    AuthorizationRefreshService,
)
from standard_annotation_backend.services.job_service import Job, JobService

FIXTURES = Path(__file__).parents[1] / "fixtures"
USERS_YAML = b"""
- accounts: {github: curator}
  authorizations:
    sabe: [{role: edit, scope: group, group: MGI}]
"""
INVALID_USERS_YAML = b"""
- accounts: {github: curator}
  authorizations:
    sabe: [{role: owner, scope: group, group: MGI}]
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
    """

    job_type: JobType
    source_key: str
    content: bytes
    invalid_content: bytes
    invalid_code: JobFailureCode
    applied_action: AuditAction
    public_error: str


CASES = {
    "entity": KindCase(
        job_type=JobType.ENTITY_REFRESH,
        source_key="mgi",
        content=(FIXTURES / "gpi" / "minimal.gpi").read_bytes(),
        invalid_content=(FIXTURES / "gpi" / "invalid-row.gpi").read_bytes(),
        invalid_code=JobFailureCode.NO_VALID_ROWS,
        applied_action=AuditAction.ENTITY_REFRESHED,
        public_error="Entity refresh failed",
    ),
    "ontology": KindCase(
        job_type=JobType.ONTOLOGY_REFRESH,
        source_key="go",
        content=(FIXTURES / "ontology" / "minimal-go.obo").read_bytes(),
        invalid_content=(FIXTURES / "ontology" / "invalid-reference.obo").read_bytes(),
        invalid_code=JobFailureCode.INVALID_DOCUMENT,
        applied_action=AuditAction.ONTOLOGY_REFRESHED,
        public_error="Ontology refresh failed",
    ),
    "authorization": KindCase(
        job_type=JobType.AUTHORIZATION_REFRESH,
        source_key="go-site",
        content=USERS_YAML,
        invalid_content=INVALID_USERS_YAML,
        invalid_code=JobFailureCode.INVALID_DOCUMENT,
        applied_action=AuditAction.AUTHORIZATION_REFRESHED,
        public_error="Authorization refresh failed",
    ),
    "rules": KindCase(
        job_type=JobType.RULES_REFRESH,
        source_key="go-site",
        content=rule_files(),
        invalid_content=rule_files({GOREFS_PATH: b"- id: GO_REF:15\n"}),
        invalid_code=JobFailureCode.INVALID_DOCUMENT,
        applied_action=AuditAction.RULES_REFRESHED,
        public_error="Rules refresh failed",
    ),
}


@pytest.fixture(params=sorted(CASES))
def case(request: pytest.FixtureRequest) -> KindCase:
    """Run each test once per refresh kind."""
    return CASES[request.param]


ANNOTATION_CASE = KindCase(
    job_type=JobType.ANNOTATION_REFRESH,
    source_key="mgi-gpad",
    content=gpad_bytes(gpad_row("UniProtKB:P12345")),
    invalid_content=b"not gpad\n",
    invalid_code=JobFailureCode.HEADER,
    applied_action=AuditAction.ANNOTATION_REFRESH_PUBLISHED,
    public_error="Annotation refresh failed",
)
"""The GPAD kind, which only the tests that name it cover."""

STORED_RECORDS: dict[str, type[Base]] = {
    "annotation": AnnotationImportRecord,
    "authorization": AuthorizationRefreshRecord,
    "entity": EntitySnapshotDetailRecord,
    "ontology": OntologySnapshotDetailRecord,
    "rules": RulesSnapshotDetailRecord,
}
"""The record each kind stores once for every document it applies.

A snapshot kind stores one detail row for each snapshot it stores.
"""


def _job(factory: UnitOfWorkFactory, case: KindCase, **parameters: object) -> UUID:
    return create_job(
        factory,
        job_type=case.job_type,
        requested_by="curator",
        parameters=parameters or {"source_key": case.source_key},
    ).job_id


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


def _row_count(session_factory: sessionmaker[Session], model: type[Base]) -> int:
    with session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_refresh_applies_once_then_reports_unchanged(
    case: KindCase,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
) -> None:
    """A new document is applied; refreshing the same document applies nothing."""
    fetchers = FakeFetchers({case.source_key: case.content})
    runner = build_runner(database_engine, unit_of_work_factory, fetchers)

    first = _job(unit_of_work_factory, case)
    runner.run(first)
    second = _job(unit_of_work_factory, case)
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
        JobFailureCode.TIMEOUT,
        JobFailureCode.HTTP_STATUS,
        JobFailureCode.SOURCE_ERROR,
    ],
)
def test_source_failure_is_terminal_with_its_code(
    case: KindCase,
    code: JobFailureCode,
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
        {"source_key": "https://user:secret@example.org/feed"},
        {"source_key": "go\n"},
        {"source_key": 7},
        {"source_key": "go", "other": 1},
        {"ontology": "go"},
        {},
    ],
)
def test_invalid_parameters_fail_without_fetching(
    monkeypatch: pytest.MonkeyPatch,
    case: KindCase,
    parameters: dict[str, object],
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """Parameters other than exactly one valid `source_key` fail the job.

    The job records only the failure code, and the log names the job and the code
    but never the stored parameter values.
    """
    log_records: list[str] = []

    def record_log(message: str, *args: object) -> None:
        log_records.append(message % args)

    monkeypatch.setattr(refresh_runner.logger, "error", record_log)
    fetchers = FakeFetchers({})
    runner = build_runner(database_engine, unit_of_work_factory, fetchers)
    job_id = create_job(
        unit_of_work_factory,
        job_type=case.job_type,
        requested_by="curator",
        parameters=parameters,
    ).job_id

    runner.run(job_id)

    record = _read(unit_of_work_factory, job_id)
    assert record.status == JobStatus.FAILED
    assert record.error == case.public_error
    assert record.progress == {"phase": "failed", "failure_code": "invalid_parameters"}
    assert fetchers.calls == []
    logs = "\n".join(log_records)
    assert str(job_id) in logs
    assert "failure_code=invalid_parameters" in logs
    for value in parameters.values():
        if isinstance(value, str) and len(value.strip()) > 2:
            assert value not in logs


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


def test_two_kinds_claiming_one_job_type_are_rejected(
    unit_of_work_factory: UnitOfWorkFactory,
    database_engine: Engine,
) -> None:
    """Each job type is run by exactly one kind, so ambiguity fails at startup."""
    authorization = AuthorizationRefreshService()

    with pytest.raises(ValueError, match="more than one refresh kind"):
        RefreshRunner(
            try_lock=bind_try_lock(database_engine),
            jobs=JobService(unit_of_work_factory),
            unit_of_work_factory=unit_of_work_factory,
            fetchers=FakeFetchers({}),
            sources=TEST_SOURCES,
            kinds=(authorization, authorization),
        )


def test_an_error_partway_through_a_load_stores_nothing_and_the_retry_loads_again(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A load that fails after storing some rows leaves no snapshot and a running job.

    The failure comes partway through storing the closure rows. Running the job
    again, as a redelivered task would, loads the document from the start and
    succeeds.
    """
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.ONTOLOGY_REFRESH,
        requested_by="scheduler",
        parameters={"source_key": "go"},
    )
    runner = build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers(
            {
                "go": obo(
                    OboTerm("GO:0000001"), OboTerm("GO:0000002", is_a=("GO:0000001",))
                )
            }
        ),
    )
    real_closure = ontology_refresh_service.compute_closure

    def failing_closure(snapshot: OntologySnapshot) -> Iterator[OntologyClosureRow]:
        rows = real_closure(snapshot)
        yield next(rows)
        raise RuntimeError("connection lost")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ontology_refresh_service, "compute_closure", failing_closure)
        with pytest.raises(RuntimeError, match="connection lost"):
            runner.run(job.job_id)

    assert JobService(unit_of_work_factory).find(job.job_id).status is JobStatus.RUNNING
    assert snapshot_count(session_factory, SnapshotKind.ONTOLOGY) == 0

    runner.run(job.job_id)

    finished = JobService(unit_of_work_factory).find(job.job_id)
    assert finished.status is JobStatus.SUCCEEDED
    assert finished.result is not None and finished.result["unchanged"] is False
    assert snapshot_count(session_factory, SnapshotKind.ONTOLOGY) == 1


@pytest.mark.parametrize("kind", sorted(STORED_RECORDS))
def test_what_a_refresh_stores_commits_only_with_its_success(
    kind: str,
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh whose success cannot be recorded stores nothing.

    The run raises and the job stays running, with no applied-refresh audit event
    and no stored snapshot, refresh, or import record. Running the job again
    applies the document once.
    """
    applied = ANNOTATION_CASE if kind == "annotation" else CASES[kind]
    stored = STORED_RECORDS[kind]
    runner = build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({applied.source_key: applied.content}),
    )
    job_id = _job(unit_of_work_factory, applied)
    original = JobService.record_success
    failures = [RuntimeError("connection lost")]

    def fail_once(self: JobService, *args: Any, **kwargs: Any) -> Job:
        if failures:
            raise failures.pop()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(JobService, "record_success", fail_once)

    with pytest.raises(RuntimeError, match="connection lost"):
        runner.run(job_id)

    assert _read(unit_of_work_factory, job_id).status == JobStatus.RUNNING
    assert _count(session_factory, applied.applied_action) == 0
    assert _row_count(session_factory, stored) == 0

    runner.run(job_id)

    finished = _read(unit_of_work_factory, job_id)
    assert finished.status == JobStatus.SUCCEEDED
    assert finished.result is not None and finished.result["unchanged"] is False
    assert _count(session_factory, applied.applied_action) == 1
    assert _row_count(session_factory, stored) == 1


def test_follow_up_jobs_are_sent_after_the_refresh_commits(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """An activation's replacement job is sent once the refresh has succeeded."""
    statuses_when_sent: list[JobStatus] = []
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.ONTOLOGY_REFRESH,
        requested_by="curator",
        parameters={"source_key": "go"},
    )

    def send(follow_up: Job) -> None:
        assert follow_up.job_type is JobType.TERM_REPLACEMENT
        with session_factory() as session:
            refresh = session.get(JobRecord, job.job_id)
            assert refresh is not None
            statuses_when_sent.append(refresh.status)

    build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({"go": obo(OboTerm("GO:0000001"))}),
        dispatch_follow_up=send,
    ).run(job.job_id)

    assert statuses_when_sent == [JobStatus.SUCCEEDED]


def _activate_go(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    dispatch: Callable[[Job], None],
) -> Job:
    """Run an ontology refresh that activates GO, sending follow-ups to `dispatch`."""
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.ONTOLOGY_REFRESH,
        requested_by="curator",
        parameters={"source_key": "go"},
    )
    build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({"go": obo(OboTerm("GO:0000001"))}),
        dispatch_follow_up=dispatch,
    ).run(job.job_id)
    return JobService(unit_of_work_factory).find(job.job_id)


def _replacement_jobs(session_factory: sessionmaker[Session]) -> list[JobRecord]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(JobRecord).where(JobRecord.job_type == JobType.TERM_REPLACEMENT)
            )
        )


def _broken(_job: Job) -> None:
    raise RuntimeError("broker unavailable")


def test_a_new_follow_up_that_cannot_be_sent_fails_and_the_refresh_succeeds(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A replacement job the refresh created fails with `dispatch_failed`."""
    refresh = _activate_go(database_engine, unit_of_work_factory, _broken)

    assert refresh.status is JobStatus.SUCCEEDED
    (replacement,) = _replacement_jobs(session_factory)
    assert replacement.status is JobStatus.FAILED
    assert replacement.error == "Term replacement could not be dispatched"
    assert replacement.progress == {
        "phase": "failed",
        "failure_code": "dispatch_failed",
    }


def test_a_joined_follow_up_that_cannot_be_sent_stays_queued(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A replacement job queued before the refresh joined it is left queued."""
    with unit_of_work_factory() as uow:
        earlier = queue_or_join(
            uow,
            job_type=JobType.TERM_REPLACEMENT,
            group_key=None,
            requested_by="cli",
        ).job
        uow.commit()

    refresh = _activate_go(database_engine, unit_of_work_factory, _broken)

    assert refresh.status is JobStatus.SUCCEEDED
    (replacement,) = _replacement_jobs(session_factory)
    assert replacement.job_id == earlier.job_id
    assert replacement.status is JobStatus.QUEUED


def test_a_follow_up_whose_failure_cannot_be_recorded_stays_queued(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If recording `dispatch_failed` fails too, the refresh still succeeds.

    The replacement job stays queued for the next trigger to join and send.
    """

    def lose_database(*_args: object, **_kwargs: object) -> Job:
        raise ConnectionResetError("database lost")

    monkeypatch.setattr(JobService, "record_failure", lose_database)

    refresh = _activate_go(database_engine, unit_of_work_factory, _broken)

    assert refresh.status is JobStatus.SUCCEEDED
    (replacement,) = _replacement_jobs(session_factory)
    assert replacement.status is JobStatus.QUEUED


def test_progress_reported_during_apply_commits_before_the_job_does(
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another connection sees a job's progress while its transaction is still open.

    The entity refresh reports `activating` after its rows are stored, and those
    rows refer to the job, so the report must not wait for the job's transaction.
    """
    seen: list[object] = []
    original = SnapshotRepository.activate

    def observe_then_activate(
        self: SnapshotRepository, record: ReferenceSnapshotRecord
    ) -> ReferenceSnapshotRecord | None:
        seen.append(
            JobService(unit_of_work_factory).find(record.job_id).progress["phase"]
        )
        return original(self, record)

    monkeypatch.setattr(SnapshotRepository, "activate", observe_then_activate)
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.ENTITY_REFRESH,
        requested_by="scheduler",
        parameters={"source_key": "mgi"},
    )
    build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({"mgi": gpi_bytes("MGI:MGI:1")}),
    ).run(job.job_id)

    assert seen == ["activating"]
    assert JobService(unit_of_work_factory).find(job.job_id).status is (
        JobStatus.SUCCEEDED
    )


@pytest.mark.parametrize("kind", ["entity", "ontology", "rules"])
def test_snapshot_kinds_report_storing_then_activating_for_a_new_snapshot(
    kind: str,
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh that stores a new snapshot reports `storing`, then `activating`.

    The job's progress names `storing` when the snapshot is created and
    `activating` when it becomes the source's active snapshot.
    """
    case = CASES[kind]
    seen: list[str] = []
    original_create = SnapshotRepository.create
    original_activate = SnapshotRepository.activate

    def phase_of(job_id: UUID) -> str:
        return str(JobService(unit_of_work_factory).find(job_id).progress["phase"])

    def observe_create(
        self: SnapshotRepository, *args: Any, **kwargs: Any
    ) -> ReferenceSnapshotRecord:
        record = original_create(self, *args, **kwargs)
        seen.append(phase_of(record.job_id))
        return record

    def observe_activate(
        self: SnapshotRepository, record: ReferenceSnapshotRecord
    ) -> ReferenceSnapshotRecord | None:
        seen.append(phase_of(record.job_id))
        return original_activate(self, record)

    monkeypatch.setattr(SnapshotRepository, "create", observe_create)
    monkeypatch.setattr(SnapshotRepository, "activate", observe_activate)
    job_id = _job(unit_of_work_factory, case)

    build_runner(
        database_engine,
        unit_of_work_factory,
        FakeFetchers({case.source_key: case.content}),
    ).run(job_id)

    assert seen == ["storing", "activating"]
    assert _read(unit_of_work_factory, job_id).status is JobStatus.SUCCEEDED
