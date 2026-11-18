"""Verify storing, activation, and replacement of entity catalogs in PostgreSQL."""

from collections.abc import Callable
from uuid import UUID

import pytest
from refresh_helpers import (
    apply_in_transaction,
    source_document,
    source_gpi,
    start_job,
)
from seeding import insert_annotation
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    ChangeSource,
)
from standard_annotation_backend.domain.entities import (
    STORED_ENTITY_REFRESH_RESULT,
    EntityRefreshResult,
)
from standard_annotation_backend.domain.jobs import JobFailureCode, JobType
from standard_annotation_backend.domain.ontology import GO_ONTOLOGY_KEY, OntologyCatalog
from standard_annotation_backend.domain.refresh import (
    NoValidRowsError,
    SourceDocument,
)
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationVersionRecord,
    ReferenceSnapshotRecord,
    StoredEntityRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import Job, JobService


def _document(source: str, *identifiers: str) -> SourceDocument:
    return source_document(source, source_gpi(*identifiers).encode())


def refresh_catalog(
    service: EntityRefreshService,
    factory: UnitOfWorkFactory,
    *identifiers: str,
    source: str = "mgi",
) -> Job:
    """Start a job and refresh `source` with a catalog of `identifiers`."""
    job = start_job(factory, JobType.ENTITY_REFRESH, source)
    apply_in_transaction(factory, service, job, _document(source, *identifiers))
    return job


def stored_result(factory: UnitOfWorkFactory, job: Job) -> EntityRefreshResult | None:
    """Return the refresh result stored for `job`, or `None` if it has none."""
    result = JobService(factory).find(job.job_id).result
    return None if result is None else STORED_ENTITY_REFRESH_RESULT.load(result)


def active_ids(session_factory: sessionmaker[Session]) -> list[str]:
    """Return every active entity identifier in sorted order."""
    with session_factory() as session:
        return list(
            session.scalars(
                select(StoredEntityRecord.db_object_id)
                .join(
                    ReferenceSnapshotRecord,
                    ReferenceSnapshotRecord.snapshot_id
                    == StoredEntityRecord.snapshot_id,
                )
                .where(
                    ReferenceSnapshotRecord.kind == SnapshotKind.ENTITY,
                    ReferenceSnapshotRecord.active.is_(True),
                )
                .order_by(StoredEntityRecord.db_object_id)
            )
        )


def _annotation_on(db_object_id: str) -> Annotation:
    """Return a structurally valid annotation whose subject is `db_object_id`."""
    return Annotation.model_validate(
        {
            "db_object_id": db_object_id,
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0006915",
            "references": ["PMID:1"],
            "evidence_type": "ECO:0000314",
            "annotation_date": "2026-09-29",
            "assigned_by": "MGI",
        }
    )


def test_replacement_does_not_change_annotations(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Replacing a catalog leaves annotations on removed entities unchanged.

    The annotations' current state and full history are the same afterwards,
    including a deleted annotation's.
    """
    service = EntityRefreshService()
    old = refresh_catalog(service, unit_of_work_factory, "MGI:z", "MGI:a", "MGI:keep")
    old_result = stored_result(unit_of_work_factory, old)
    assert old_result is not None
    refresh_catalog(service, unit_of_work_factory, "RGD:1", source="rgd")
    ids = [UUID(int=3), UUID(int=1), UUID(int=2), UUID(int=4)]
    with unit_of_work_factory() as uow:
        for index, annotation_id in enumerate(ids):
            annotation = Annotation.model_validate(
                {
                    "db_object_id": "MGI:z" if index == 0 else "MGI:a",
                    "relation": "RO:0002331",
                    "ontology_class_id": "GO:0008150",
                    "references": [f"PMID:{index + 1}"],
                    "evidence_type": "ECO:0000314",
                    "annotation_date": "2026-09-29",
                    "assigned_by": "MGI",
                }
            )
            insert_annotation(
                uow.annotations.session,
                annotation=annotation,
                actor_id="curator",
                change_source=ChangeSource.API,
                owning_group_id="MGI",
                record_origin=AnnotationOrigin.DIRECT,
                annotation_id=annotation_id,
            )
        deleted = uow.annotations.soft_delete(
            UUID(int=4),
            expected_version=1,
            actor_id="curator",
            change_source=ChangeSource.API,
        )
        assert deleted.status == "deleted"
        assert deleted.current_version == 2
        uow.commit()
    annotation_query = select(AnnotationRecord.__table__).order_by(
        AnnotationRecord.annotation_id
    )
    history_query = select(AnnotationVersionRecord.__table__).order_by(
        AnnotationVersionRecord.annotation_id, AnnotationVersionRecord.version
    )
    with session_factory() as session:
        annotations_before = list(session.execute(annotation_query).mappings())
        history_before = list(session.execute(history_query).mappings())
    new = start_job(unit_of_work_factory, JobType.ENTITY_REFRESH, "mgi")
    outcome = apply_in_transaction(
        unit_of_work_factory, service, new, _document("mgi", "MGI:new", "MGI:keep")
    )
    result = STORED_ENTITY_REFRESH_RESULT.load(outcome.result)
    assert (result.added_count, result.retained_count, result.removed_count) == (
        1,
        1,
        2,
    )
    assert active_ids(session_factory) == ["MGI:keep", "MGI:new", "RGD:1"]
    assert stored_result(unit_of_work_factory, old) == old_result
    with session_factory() as session:
        assert list(session.execute(annotation_query).mappings()) == annotations_before
        assert list(session.execute(history_query).mappings()) == history_before


@pytest.mark.parametrize(
    ("text", "issue_count"),
    [
        pytest.param(source_gpi(), 0, id="header-only"),
        pytest.param(source_gpi() + "MGI:9\tshort\n", 1, id="all-invalid"),
        pytest.param(source_gpi("MGI:9", "MGI:9"), 2, id="only-repeats"),
    ],
)
def test_file_without_valid_rows_fails_and_keeps_the_catalogs(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    text: str,
    issue_count: int,
) -> None:
    """A file with no row that can be loaded fails with `no_valid_rows`.

    That includes a file with no data rows. The failure details count every
    rejected row, and every active catalog is unchanged.
    """
    service = EntityRefreshService()
    for prefix, key in (("MGI", "mgi"), ("RGD", "rgd")):
        refresh_catalog(service, unit_of_work_factory, f"{prefix}:1", source=key)
    job = start_job(unit_of_work_factory, JobType.ENTITY_REFRESH, "mgi")

    with pytest.raises(NoValidRowsError) as raised:
        apply_in_transaction(
            unit_of_work_factory, service, job, source_document("mgi", text.encode())
        )

    assert raised.value.failure_code is JobFailureCode.NO_VALID_ROWS
    assert raised.value.failure_details is not None
    assert raised.value.failure_details["issue_count"] == issue_count
    assert active_ids(session_factory) == ["MGI:1", "RGD:1"]


def test_catalog_spanning_multiple_insert_batches_keeps_every_record(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A catalog larger than one insert batch is stored with every record."""
    service = EntityRefreshService()
    job = refresh_catalog(
        service, unit_of_work_factory, *(f"MGI:{index}" for index in range(5001))
    )
    result = stored_result(unit_of_work_factory, job)
    assert result is not None
    assert (result.data_rows, result.entity_count) == (5001, 5001)
    assert len(active_ids(session_factory)) == 5001


def test_validation_reads_each_active_entity_taxon_and_type(
    unit_of_work_factory: UnitOfWorkFactory,
    reference_data: Callable[..., None],
) -> None:
    """Validation reads the taxon and type each entity's GPI line gives."""
    service = EntityRefreshService()
    text = (
        "!gpi-version: 2.0\n!generated-by: TestDB\n!date-generated: 2026-09-29\n"
        "MGI:1\tA\tProtein\t\tPR:000000001\tNCBITaxon:10090\t\tMGI:1\t\t\t\n"
        "MGI:2\tB\tComplex\t\tGO:0032991\tNCBITaxon:9606\t\tMGI:2\t\t\t\n"
    )
    job = start_job(unit_of_work_factory, JobType.ENTITY_REFRESH, "mgi")

    apply_in_transaction(
        unit_of_work_factory, service, job, source_document("mgi", text.encode())
    )

    with unit_of_work_factory() as uow:
        facts = uow.validation_facts.gather(
            [_annotation_on("MGI:1"), _annotation_on("MGI:2")],
            [(), ()],
            OntologyCatalog({GO_ONTOLOGY_KEY: ("GO",)}),
        )
    assert [
        None
        if fact.entity is None
        else (fact.entity.db_object_taxon_id, fact.entity.db_object_type)
        for fact in facts
    ] == [("NCBITaxon:10090", "PR:000000001"), ("NCBITaxon:9606", "GO:0032991")]
