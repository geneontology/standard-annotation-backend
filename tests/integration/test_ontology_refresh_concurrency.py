"""Verify ontology refreshes coordinate safely across PostgreSQL connections."""

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import UUID

import httpx2
import pytest
from refresh_helpers import (
    REVISION,
    OboTerm,
    go_document,
    ignore_progress,
    obo,
    start_job,
)
from seeding import create_job, insert_annotation
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.config import get_settings
from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    ChangeSource,
)
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.ontology import (
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
    OntologyTerm,
)
from standard_annotation_backend.domain.refresh import SourceDocument
from standard_annotation_backend.persistence.locks import (
    GLOBAL_ANNOTATION_WRITE_LOCK_KEY,
    LockNamespace,
    acquire_global_annotation_write_lock,
    bind_try_lock,
    try_advisory_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    JobRecord,
    OntologyMetadataRecord,
)
from standard_annotation_backend.persistence.repositories import (
    AnnotationRepository,
    OntologyRepository,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshService,
)
from standard_annotation_backend.workers import tasks
from standard_annotation_backend.workers.tasks import (
    WorkerTaskUnavailableError,
    prune_ontology_snapshots,
)

OBO = (
    Path(__file__).parents[1] / "fixtures" / "ontology" / "minimal-go.obo"
).read_bytes()


@pytest.fixture(autouse=True)
def _active_subjects(seed_active_subjects: Callable[..., None]) -> None:
    """Make the subjects these tests write active in the entity catalog.

    Direct annotation writes require an active subject.
    """
    seed_active_subjects("UniProtKB:P12345", "UniProtKB:Q12345")


OLD_JOB_ID = UUID("00000000-0000-0000-0000-000000000071")
OLD_TERM = "GO:0000001"
NEW_TERM = "GO:0000002"
OTHER_TERM = "GO:0000003"
REPLACED_ANNOTATION_ID = UUID("00000000-0000-0000-0000-000000000073")
OTHER_ANNOTATION_ID = UUID("00000000-0000-0000-0000-000000000074")
NOW = datetime(2026, 9, 28, 18, tzinfo=UTC)
FAILURE_GUARD_SECONDS = 5


def _document(revision: str) -> OntologyDocument:
    return OntologyDocument(
        ontology_key=OntologyKey.GO,
        content=revision.encode(),
        source_type="test",
        source_locator="fixture/go.obo",
        source_revision=revision,
        source_checksum=("a" if revision == "old" else "b") * 64,
        fetched_at=NOW,
    )


def _snapshot(revision: str, terms: dict[str, OntologyTerm]) -> OntologySnapshot:
    return OntologySnapshot(_document(revision), revision, (), terms, ())


def _annotation(term_id: str, *, object_id: str) -> Annotation:
    return Annotation.model_validate(
        {
            "db_object_id": object_id,
            "negation": None,
            "relation": "RO:0002331",
            "ontology_class_id": term_id,
            "references": ["PMID:1"],
            "evidence_type": "ECO:0000314",
            "with_or_from": [],
            "interacting_taxon_id": [],
            "annotation_date": "2026-09-28",
            "assigned_by": "GO_Central",
            "annotation_extensions": [],
        }
    )


def _prepare_load(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> tuple[Job, SourceDocument]:
    """Activate an old snapshot with two annotations and start a refresh job.

    Returns:
        The started job and the document that replaces the first annotation's
        obsolete term.
    """
    with session_factory() as session:
        session.add(
            JobRecord(
                job_id=OLD_JOB_ID,
                job_type="ontology_refresh",
                status="queued",
                requested_by="test",
                parameters={"source_key": "go"},
                progress={},
                warnings=[],
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    old = _snapshot("old", {OLD_TERM: OntologyTerm(OLD_TERM, False, (), ())})
    with unit_of_work_factory() as unit_of_work:
        old_record = unit_of_work.ontologies.stage(
            job_id=OLD_JOB_ID,
            document=old.document,
            snapshot=old,
            closure_rows=(),
        )
        unit_of_work.ontologies.activate(old_record.version_id)
        insert_annotation(
            unit_of_work.annotations.session,
            annotation=_annotation(OLD_TERM, object_id="UniProtKB:P12345"),
            actor_id="creator",
            change_source=ChangeSource.API,
            owning_group_id="group-1",
            record_origin=AnnotationOrigin.DIRECT,
            annotation_id=REPLACED_ANNOTATION_ID,
        )
        insert_annotation(
            unit_of_work.annotations.session,
            annotation=_annotation(OTHER_TERM, object_id="UniProtKB:Q12345"),
            actor_id="creator",
            change_source=ChangeSource.API,
            owning_group_id="group-1",
            record_origin=AnnotationOrigin.DIRECT,
            annotation_id=OTHER_ANNOTATION_ID,
        )
        unit_of_work.commit()

    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    document = go_document(
        obo(
            OboTerm(OLD_TERM, obsolete=True, replaced_by=(NEW_TERM,)),
            OboTerm(NEW_TERM),
            OboTerm(OTHER_TERM),
        )
    )
    return job, document


def _active_state(
    unit_of_work_factory: UnitOfWorkFactory,
) -> tuple[str | None, str, int]:
    with unit_of_work_factory() as unit_of_work:
        active = unit_of_work.ontologies.get_active(OntologyKey.GO)
        annotation = unit_of_work.annotations.get(REPLACED_ANNOTATION_ID)
        assert active is not None
        assert annotation is not None
        return (
            active.source_revision,
            annotation.ontology_class_id,
            annotation.current_version,
        )


def _wait_for_global_lock_contention(
    observer: Session, *, holder_pid: int, waiter_pid: int
) -> None:
    lock_key = GLOBAL_ANNOTATION_WRITE_LOCK_KEY
    class_id = (lock_key >> 32) & 0xFFFFFFFF
    object_id = lock_key & 0xFFFFFFFF
    expected = {
        (holder_pid, "ExclusiveLock", True),
        (waiter_pid, "ShareLock", False),
    }
    deadline = monotonic() + FAILURE_GUARD_SECONDS
    observed: set[tuple[int, str, bool]] = set()
    statement = text(
        """
        SELECT pid, mode, granted
        FROM pg_locks
        WHERE locktype = 'advisory'
          AND classid = :class_id
          AND objid = :object_id
          AND objsubid = 1
          AND pid IN (:holder_pid, :waiter_pid)
        """
    )
    while monotonic() < deadline:
        observed = {
            (row.pid, row.mode, row.granted)
            for row in observer.execute(
                statement,
                {
                    "class_id": class_id,
                    "object_id": object_id,
                    "holder_pid": holder_pid,
                    "waiter_pid": waiter_pid,
                },
            )
        }
        if observed == expected:
            return
    raise AssertionError(f"global annotation lock contention not observed: {observed}")


def test_pruning_task_retries_until_same_ontology_refresh_lock_is_available(
    configured_environment: None,
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pruning retries while a refresh holds the same ontology lock, then runs."""
    monkeypatch.setenv("SAB_DATABASE_URL", os.environ["SAB_TEST_DATABASE_URL"])
    get_settings.cache_clear()
    calls: list[datetime] = []

    def record_pruning(
        _repository: OntologyRepository, key: OntologyKey, pruned_at: datetime
    ) -> tuple[UUID, ...]:
        calls.append(pruned_at)
        return ()

    monkeypatch.setattr(OntologyRepository, "prune_candidates", record_pruning)

    with try_advisory_lock(
        database_engine, LockNamespace.ONTOLOGY, OntologyKey.GO.value
    ) as acquired:
        assert acquired is True
        with pytest.raises(WorkerTaskUnavailableError):
            prune_ontology_snapshots.run(OntologyKey.GO.value)
        assert calls == []

    prune_ontology_snapshots.run(OntologyKey.GO.value)

    assert len(calls) == 1


def test_refresh_task_retries_while_the_ontology_lock_is_held_then_succeeds(
    configured_environment: None,
    database_engine: Engine,
    unit_of_work_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Celery task requests a retry while another process refreshes GO.

    The job stays running while it waits, and a later delivery finishes it.
    """
    monkeypatch.setenv("SAB_DATABASE_URL", os.environ["SAB_TEST_DATABASE_URL"])
    get_settings.cache_clear()
    monkeypatch.setattr(prune_ontology_snapshots, "delay", lambda _key: None)
    # Replace only the network: resolving the ref, then fetching the OBO bytes.
    responses = [
        httpx2.Response(200, json={"sha": "a" * 40}),
        httpx2.Response(200, content=OBO),
    ]

    def handle_request(
        _transport: httpx2.HTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
        response = responses.pop(0)
        return httpx2.Response(
            response.status_code, content=response.content, request=request
        )

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", handle_request)
    job_id = create_job(
        unit_of_work_factory,
        job_type=JobType.ONTOLOGY_REFRESH,
        requested_by="test",
        parameters={"source_key": "go"},
    ).job_id

    try:
        with try_advisory_lock(
            database_engine, LockNamespace.ONTOLOGY, OntologyKey.GO.value
        ) as acquired:
            assert acquired is True
            with pytest.raises(WorkerTaskUnavailableError):
                tasks.run_refresh.run(str(job_id))
        with unit_of_work_factory() as unit_of_work:
            record = unit_of_work.jobs.get(job_id)
            assert record is not None
            assert record.status == JobStatus.RUNNING.value

        # The first attempt fetched and then waited for the lock, so the retry
        # fetches again with a fresh pair of responses.
        responses.extend(
            [
                httpx2.Response(200, json={"sha": "a" * 40}),
                httpx2.Response(200, content=OBO),
            ]
        )
        tasks.run_refresh.run(str(job_id))
    finally:
        get_settings.cache_clear()

    with unit_of_work_factory() as unit_of_work:
        record = unit_of_work.jobs.get(job_id)
        assert record is not None
        assert record.status == JobStatus.SUCCEEDED.value


def test_activation_is_atomically_visible_and_blocks_ordinary_writes(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readers see complete states while an ordinary writer waits for activation."""
    job, document = _prepare_load(unit_of_work_factory, session_factory)
    service = OntologyRefreshService(
        unit_of_work_factory, bind_try_lock(database_engine), None
    )
    update_flushed = Event()
    allow_activation_commit = Event()
    writer_started = Event()
    activation_pid: list[int] = []
    writer_pid: list[int] = []
    original = AnnotationRepository.apply_system_update

    def pause_after_update(
        self: AnnotationRepository,
        record: AnnotationRecord,
        annotation: Annotation,
        *,
        actor_id: str,
        change_source: ChangeSource,
    ) -> AnnotationRecord:
        updated = original(
            self,
            record,
            annotation,
            actor_id=actor_id,
            change_source=change_source,
        )
        pid = self.session.scalar(text("SELECT pg_backend_pid()"))
        assert pid is not None
        activation_pid.append(pid)
        update_flushed.set()
        if not allow_activation_commit.wait(FAILURE_GUARD_SECONDS):
            raise TimeoutError("activation test was not released")
        return updated

    def ordinary_write(session: Session) -> str:
        pid = session.scalar(text("SELECT pg_backend_pid()"))
        assert pid is not None
        writer_pid.append(pid)
        writer_started.set()
        acquire_global_annotation_write_lock(session)
        observed = AnnotationRepository(session).get(REPLACED_ANNOTATION_ID)
        assert observed is not None
        other = AnnotationRepository(session).get(OTHER_ANNOTATION_ID)
        assert other is not None
        changed_data = dict(other.annotation_data)
        changed_data["assigned_by"] = "Writer_After_Load"
        AnnotationRepository(session).update(
            OTHER_ANNOTATION_ID,
            Annotation.model_validate(changed_data),
            expected_version=1,
            actor_id="writer",
        )
        session.commit()
        return observed.ontology_class_id

    monkeypatch.setattr(AnnotationRepository, "apply_system_update", pause_after_update)
    with (
        session_factory() as writer_session,
        session_factory() as observer_session,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        activation = executor.submit(service.apply, job, document, ignore_progress)
        assert update_flushed.wait(FAILURE_GUARD_SECONDS)
        assert _active_state(unit_of_work_factory) == ("old", OLD_TERM, 1)

        writer = executor.submit(ordinary_write, writer_session)
        assert writer_started.wait(FAILURE_GUARD_SECONDS)
        _wait_for_global_lock_contention(
            observer_session,
            holder_pid=activation_pid[0],
            waiter_pid=writer_pid[0],
        )
        assert not writer.done()
        allow_activation_commit.set()
        outcome = activation.result(timeout=FAILURE_GUARD_SECONDS)
        observed_by_writer = writer.result(timeout=FAILURE_GUARD_SECONDS)

    assert outcome.result["annotation_update_count"] == 1
    assert observed_by_writer == NEW_TERM
    assert _active_state(unit_of_work_factory) == (REVISION, NEW_TERM, 2)


def test_failed_activation_never_exposes_partial_state(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader during and after a failed activation sees the prior complete state."""
    job, document = _prepare_load(unit_of_work_factory, session_factory)
    service = OntologyRefreshService(
        unit_of_work_factory, bind_try_lock(database_engine), None
    )
    update_flushed = Event()
    allow_failure = Event()
    original = AnnotationRepository.apply_system_update

    def fail_after_update(
        self: AnnotationRepository,
        record: AnnotationRecord,
        annotation: Annotation,
        *,
        actor_id: str,
        change_source: ChangeSource,
    ) -> AnnotationRecord:
        original(
            self,
            record,
            annotation,
            actor_id=actor_id,
            change_source=change_source,
        )
        update_flushed.set()
        if not allow_failure.wait(FAILURE_GUARD_SECONDS):
            raise TimeoutError("failure test was not released")
        raise RuntimeError("injected activation failure")

    monkeypatch.setattr(AnnotationRepository, "apply_system_update", fail_after_update)
    with ThreadPoolExecutor(max_workers=1) as executor:
        activation = executor.submit(service.apply, job, document, ignore_progress)
        assert update_flushed.wait(FAILURE_GUARD_SECONDS)
        assert _active_state(unit_of_work_factory) == ("old", OLD_TERM, 1)
        allow_failure.set()
        with pytest.raises(RuntimeError, match="injected activation failure"):
            activation.result(timeout=FAILURE_GUARD_SECONDS)

    assert _active_state(unit_of_work_factory) == ("old", OLD_TERM, 1)
    with session_factory() as session:
        candidate = session.scalar(
            select(OntologyMetadataRecord).where(
                OntologyMetadataRecord.job_id == job.job_id
            )
        )
        assert candidate is not None
        assert candidate.active is False
