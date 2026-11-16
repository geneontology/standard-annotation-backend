"""Verify ontology refreshes coordinate safely across PostgreSQL connections."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID

import pytest
from refresh_helpers import (
    REVISION,
    TEST_SOURCES,
    OboTerm,
    apply_in_transaction,
    go_document,
    obo,
    start_job,
)
from seeding import insert_annotation
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    ChangeSource,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.ontology import (
    GO_ONTOLOGY_KEY,
    OntologyDocument,
    OntologySnapshot,
    OntologyTerm,
)
from standard_annotation_backend.domain.refresh import (
    SourceDocument,
    SourceProvenance,
)
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.persistence.locks import (
    acquire_global_annotation_write_lock,
)
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.ontology_refresh_service import (
    OntologyRefreshService,
)


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
        ontology_key=GO_ONTOLOGY_KEY,
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
                status="succeeded",
                requested_by="test",
                parameters={"source_key": "go"},
                progress={},
                warnings=[],
                result={},
                created_at=NOW,
                updated_at=NOW,
                started_at=NOW,
                completed_at=NOW,
            )
        )
        session.commit()

    old = _snapshot("old", {OLD_TERM: OntologyTerm(OLD_TERM, False, (), ())})
    with unit_of_work_factory() as unit_of_work:
        old_record = unit_of_work.snapshots.create(
            SnapshotKind.ONTOLOGY,
            GO_ONTOLOGY_KEY,
            OLD_JOB_ID,
            SourceProvenance(
                source_type=old.document.source_type,
                source_locator=old.document.source_locator,
                source_revision=old.document.source_revision,
                source_checksum=old.document.source_checksum,
                fetched_at=old.document.fetched_at,
            ),
        )
        unit_of_work.ontologies.insert(old_record.snapshot_id, old, ())
        unit_of_work.snapshots.activate(old_record)
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
        active = unit_of_work.snapshots.active(SnapshotKind.ONTOLOGY, GO_ONTOLOGY_KEY)
        annotation = unit_of_work.annotations.get(REPLACED_ANNOTATION_ID)
        assert active is not None
        assert annotation is not None
        return (
            active.source_revision,
            annotation.ontology_class_id,
            annotation.current_version,
        )


def test_activation_does_not_wait_for_annotation_writes(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """An activation completes while an annotation write is in progress.

    The writer holds the shared global annotation-write lock, as every annotation
    write does, on its own connection for the whole activation. The new snapshot
    becomes active and the annotation on its obsolete term is left unchanged.
    """
    job, document = _prepare_load(unit_of_work_factory, session_factory)
    service = OntologyRefreshService(TEST_SOURCES)
    with session_factory() as writer, ThreadPoolExecutor(max_workers=1) as executor:
        acquire_global_annotation_write_lock(writer)
        activation = executor.submit(
            apply_in_transaction, unit_of_work_factory, service, job, document
        )
        try:
            activation.result(timeout=FAILURE_GUARD_SECONDS)
        finally:
            # Release the lock even on timeout, so a waiting activation fails
            # the test instead of blocking the executor's shutdown.
            writer.rollback()

    assert _active_state(unit_of_work_factory) == (REVISION, OLD_TERM, 1)
