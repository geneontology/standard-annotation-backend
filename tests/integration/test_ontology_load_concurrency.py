"""Verify ontology loads coordinate safely across PostgreSQL connections."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from time import monotonic
from uuid import UUID

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.ontology import (
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
    OntologyTerm,
)
from standard_annotation_backend.ontology.registry import GO_DEFINITION
from standard_annotation_backend.persistence.annotation_data import (
    AnnotationPersistenceData,
)
from standard_annotation_backend.persistence.locks import (
    GLOBAL_ANNOTATION_WRITE_LOCK_KEY,
    acquire_global_annotation_write_lock,
    ontology_load_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationOrigin,
    AnnotationRecord,
    JobRecord,
    OntologyMetadataRecord,
)
from standard_annotation_backend.persistence.repositories import AnnotationRepository
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.ontology_load_service import (
    OntologyLoadService,
)

OLD_JOB_ID = UUID("00000000-0000-0000-0000-000000000071")
LOAD_JOB_ID = UUID("00000000-0000-0000-0000-000000000072")
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
        retrieved_at=NOW,
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
) -> OntologyLoadService:
    with session_factory() as session:
        for job_id in (OLD_JOB_ID, LOAD_JOB_ID):
            session.add(
                JobRecord(
                    job_id=job_id,
                    job_type="ontology_load",
                    status="queued",
                    requested_by="test",
                    parameters={"ontology": "go"},
                    progress={},
                    warnings=[],
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.commit()

    old = _snapshot("old", {"GO:OLD": OntologyTerm("GO:OLD", False, (), ())})
    with unit_of_work_factory() as unit_of_work:
        old_record = unit_of_work.ontologies.stage(
            job_id=OLD_JOB_ID,
            document=old.document,
            snapshot=old,
            closure_rows=(),
        )
        unit_of_work.ontologies.activate(old_record.version_id)
        unit_of_work.annotations.create(
            annotation=_annotation("GO:OLD", object_id="UniProtKB:P12345"),
            actor_id="creator",
            change_source="test",
            owning_group_id="group-1",
            record_origin=AnnotationOrigin.DIRECT,
            annotation_id=REPLACED_ANNOTATION_ID,
        )
        unit_of_work.annotations.create(
            annotation=_annotation("GO:OTHER", object_id="UniProtKB:Q12345"),
            actor_id="creator",
            change_source="test",
            owning_group_id="group-1",
            record_origin=AnnotationOrigin.DIRECT,
            annotation_id=OTHER_ANNOTATION_ID,
        )
        unit_of_work.commit()

    candidate = _snapshot(
        "new",
        {
            "GO:OLD": OntologyTerm("GO:OLD", True, ("GO:NEW",), ()),
            "GO:NEW": OntologyTerm("GO:NEW", False, (), ()),
            "GO:OTHER": OntologyTerm("GO:OTHER", False, (), ()),
        },
    )
    service = OntologyLoadService(unit_of_work_factory, GO_DEFINITION)
    service.stage(
        job_id=LOAD_JOB_ID,
        document=candidate.document,
        snapshot=candidate,
    )
    return service


def _active_state(unit_of_work_factory: UnitOfWorkFactory) -> tuple[str, str, int]:
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


def test_same_ontology_load_lock_is_released_after_success_and_failure(
    database_engine: Engine,
) -> None:
    """Only one load holds a key lock, which is released on success or failure."""
    with ontology_load_lock(database_engine, OntologyKey.GO) as acquired:
        assert acquired is True
        with ontology_load_lock(database_engine, OntologyKey.GO) as competing:
            assert competing is False
        with ontology_load_lock(database_engine, "future-ontology") as independent:
            assert independent is True
    with ontology_load_lock(database_engine, OntologyKey.GO) as recovered:
        assert recovered is True

    with (
        pytest.raises(RuntimeError, match="injected"),
        ontology_load_lock(database_engine, OntologyKey.GO) as acquired,
    ):
        assert acquired is True
        raise RuntimeError("injected")
    with ontology_load_lock(database_engine, OntologyKey.GO) as recovered:
        assert recovered is True


def test_activation_is_atomically_visible_and_blocks_ordinary_writes(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readers see complete states while an ordinary writer waits for activation."""
    service = _prepare_load(unit_of_work_factory, session_factory)
    update_flushed = Event()
    allow_activation_commit = Event()
    writer_started = Event()
    activation_pid: list[int] = []
    writer_pid: list[int] = []
    original = AnnotationRepository.apply_system_update

    def pause_after_update(
        self: AnnotationRepository,
        record: AnnotationRecord,
        persistence_data: AnnotationPersistenceData,
        *,
        actor_id: str,
        change_source: str,
    ) -> AnnotationRecord:
        updated = original(
            self,
            record,
            persistence_data,
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
        AnnotationRepository(session).update_direct(
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
        activation = executor.submit(
            service.activate, job_id=LOAD_JOB_ID, actor_id="ontology-worker"
        )
        assert update_flushed.wait(FAILURE_GUARD_SECONDS)
        assert _active_state(unit_of_work_factory) == ("old", "GO:OLD", 1)

        writer = executor.submit(ordinary_write, writer_session)
        assert writer_started.wait(FAILURE_GUARD_SECONDS)
        _wait_for_global_lock_contention(
            observer_session,
            holder_pid=activation_pid[0],
            waiter_pid=writer_pid[0],
        )
        assert not writer.done()
        allow_activation_commit.set()
        result = activation.result(timeout=FAILURE_GUARD_SECONDS)
        observed_by_writer = writer.result(timeout=FAILURE_GUARD_SECONDS)

    assert result.annotation_update_count == 1
    assert observed_by_writer == "GO:NEW"
    assert _active_state(unit_of_work_factory) == ("new", "GO:NEW", 2)


def test_failed_activation_never_exposes_partial_state(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader during and after a failed activation sees the prior complete state."""
    service = _prepare_load(unit_of_work_factory, session_factory)
    update_flushed = Event()
    allow_failure = Event()
    original = AnnotationRepository.apply_system_update

    def fail_after_update(
        self: AnnotationRepository,
        record: AnnotationRecord,
        persistence_data: AnnotationPersistenceData,
        *,
        actor_id: str,
        change_source: str,
    ) -> AnnotationRecord:
        original(
            self,
            record,
            persistence_data,
            actor_id=actor_id,
            change_source=change_source,
        )
        update_flushed.set()
        if not allow_failure.wait(FAILURE_GUARD_SECONDS):
            raise TimeoutError("failure test was not released")
        raise RuntimeError("injected activation failure")

    monkeypatch.setattr(AnnotationRepository, "apply_system_update", fail_after_update)
    with ThreadPoolExecutor(max_workers=1) as executor:
        activation = executor.submit(
            service.activate, job_id=LOAD_JOB_ID, actor_id="ontology-worker"
        )
        assert update_flushed.wait(FAILURE_GUARD_SECONDS)
        assert _active_state(unit_of_work_factory) == ("old", "GO:OLD", 1)
        allow_failure.set()
        with pytest.raises(RuntimeError, match="injected activation failure"):
            activation.result(timeout=FAILURE_GUARD_SECONDS)

    assert _active_state(unit_of_work_factory) == ("old", "GO:OLD", 1)
    with session_factory() as session:
        candidate = session.scalar(
            select(OntologyMetadataRecord).where(
                OntologyMetadataRecord.job_id == LOAD_JOB_ID
            )
        )
        assert candidate is not None
        assert candidate.active is False
