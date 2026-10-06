"""Verify that stored ontology candidates cannot be replaced or rolled back."""

from datetime import UTC, datetime, timedelta

import pytest
from refresh_helpers import start_job

from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.ontology import (
    OntologyCandidateConflictError,
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
    OntologyTerm,
    compute_closure,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory

NOW = datetime(2026, 9, 28, 15, tzinfo=UTC)


def _document(
    revision: str, checksum: str, *, fetched_at: datetime
) -> OntologyDocument:
    return OntologyDocument(
        ontology_key=OntologyKey.GO,
        content=revision.encode(),
        source_type="test",
        source_locator="fixture/go.obo",
        source_revision=revision,
        source_checksum=checksum * 64,
        fetched_at=fetched_at,
    )


def _snapshot(
    revision: str,
    checksum: str,
    term_id: str,
    *,
    fetched_at: datetime = NOW,
) -> OntologySnapshot:
    return OntologySnapshot(
        _document(revision, checksum, fetched_at=fetched_at),
        revision,
        (),
        {term_id: OntologyTerm(term_id, False, (), ())},
        (),
    )


def test_staged_job_cannot_switch_to_different_source_bytes(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A retry cannot replace the immutable candidate already owned by its job."""
    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    first = _snapshot("first", "a", "GO:0000001")
    second = _snapshot("second", "b", "GO:0000002")
    with unit_of_work_factory() as uow:
        uow.ontologies.stage(
            job_id=job.job_id,
            document=first.document,
            snapshot=first,
            closure_rows=compute_closure(first),
        )
        uow.commit()

    with (
        unit_of_work_factory() as uow,
        pytest.raises(OntologyCandidateConflictError, match="differs"),
    ):
        uow.ontologies.stage(
            job_id=job.job_id,
            document=second.document,
            snapshot=second,
            closure_rows=compute_closure(second),
        )


def test_restaging_the_same_source_returns_the_existing_candidate(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A retry that resolved the same source keeps the job's existing candidate."""
    job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    snapshot = _snapshot("first", "a", "GO:0000001")
    with unit_of_work_factory() as uow:
        staged = uow.ontologies.stage(
            job_id=job.job_id,
            document=snapshot.document,
            snapshot=snapshot,
            closure_rows=compute_closure(snapshot),
        )
        version_id = staged.version_id
        uow.commit()

    with unit_of_work_factory() as uow:
        restaged = uow.ontologies.stage(
            job_id=job.job_id,
            document=snapshot.document,
            snapshot=snapshot,
            closure_rows=compute_closure(snapshot),
        )

        assert restaged.version_id == version_id
        assert uow.ontologies.term_count(version_id) == 1


def test_older_staged_job_cannot_replace_a_newer_active_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A delayed retry cannot roll an ontology key back to an older resolution."""
    older_job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    newer_job = start_job(unit_of_work_factory, JobType.ONTOLOGY_REFRESH, "go")
    # Staging order, not fetch time, decides which candidate is newer.
    older = _snapshot("older", "a", "GO:0000001", fetched_at=NOW + timedelta(days=1))
    newer = _snapshot("newer", "b", "GO:0000001", fetched_at=NOW - timedelta(days=1))
    with unit_of_work_factory() as uow:
        for job, snapshot in ((older_job, older), (newer_job, newer)):
            uow.ontologies.stage(
                job_id=job.job_id,
                document=snapshot.document,
                snapshot=snapshot,
                closure_rows=compute_closure(snapshot),
            )
        uow.commit()
    with unit_of_work_factory() as uow:
        candidate, _ = uow.ontologies.lock_activation_state(newer_job.job_id)
        uow.ontologies.activate(candidate.version_id)
        uow.commit()

    with (
        unit_of_work_factory() as uow,
        pytest.raises(OntologyCandidateConflictError, match="newer"),
    ):
        uow.ontologies.lock_activation_state(older_job.job_id)

    with unit_of_work_factory() as uow:
        active = uow.ontologies.get_active(OntologyKey.GO)
        assert active is not None
        assert active.source_revision == "newer"
