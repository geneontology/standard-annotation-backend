"""Verify staging, publication, and replacement of entity catalogs in PostgreSQL."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.entities import (
    EntityCandidateConflictError,
    EntityCatalog,
    EntityCatalogCollisionError,
)
from standard_annotation_backend.entity_sources.http import EntitySourceDocument
from standard_annotation_backend.gpi.parser import parse_gpi
from standard_annotation_backend.persistence.models import (
    AnnotationOrigin,
    AnnotationRecord,
    AnnotationVersionRecord,
    AuditEventRecord,
    EntityCatalogSnapshotRecord,
    EntityMembershipRecord,
    EntitySourceRecord,
    EntityStagingRecord,
    JobRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.entity_import_service import (
    EntityImportService,
)

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def source_catalog(*identifiers: str) -> EntityCatalog:
    """Parse valid GPI rows, preserving repeated identifiers and source order."""
    text = "!gpi-version: 2.0\n!generated-by: TestDB\n!date-generated: 2026-09-29\n"
    text += "".join(
        f"{identifier}\tSYM{index}\tProtein\t\tSO:0001217\tNCBITaxon:9606\t\t{identifier}\t\t\t\n"
        for index, identifier in enumerate(identifiers)
    )
    document = EntitySourceDocument(
        source_key="mgi",
        source_url="https://example.org/entities.gpi",
        source_checksum=sha256(text.encode()).hexdigest(),
        fetched_at=NOW,
        text=text,
    )
    return parse_gpi(document)


def prepare_job(session_factory: sessionmaker[Session], source: str = "mgi") -> UUID:
    """Persist one running import job for a source."""
    job_id = uuid4()
    with session_factory() as session:
        session.add(
            JobRecord(
                job_id=job_id,
                job_type="entity_import",
                status="running",
                requested_by="curator",
                parameters={"source_key": source},
                progress={},
                warnings=[],
                created_at=NOW,
                updated_at=NOW,
                started_at=NOW,
            )
        )
        session.commit()
    return job_id


def stage(
    service: EntityImportService,
    session_factory: sessionmaker[Session],
    *identifiers: str,
    source: str = "mgi",
) -> UUID:
    """Create a running import job and stage a catalog of `identifiers` for it.

    Returns:
        The ID of the job the catalog was staged for.
    """
    job_id = prepare_job(session_factory, source)
    service.stage(
        job_id=job_id, source_key=source, catalog=source_catalog(*identifiers)
    )
    return job_id


def active_ids(session_factory: sessionmaker[Session]) -> list[str]:
    """Return every active entity identifier in sorted order."""
    with session_factory() as session:
        return list(
            session.scalars(
                select(EntityMembershipRecord.db_object_id).order_by(
                    EntityMembershipRecord.db_object_id
                )
            )
        )


def test_publication_retains_repeated_metadata_and_durable_result(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Publishing keeps every source row and records one audit event and result.

    Each distinct identifier becomes one active entity. Publishing or staging
    again afterward returns the same result without adding rows.
    """
    service = EntityImportService(unit_of_work_factory)
    job_id = stage(service, session_factory, "MGI:2", "MGI:1", "MGI:1")
    result = service.publish(job_id=job_id, actor_id="curator")
    assert (
        result.source_record_count,
        result.active_identifier_count,
        result.added_count,
    ) == (3, 2, 2)
    assert (result.retained_count, result.removed_count, result.removal_impacts) == (
        0,
        0,
        (),
    )
    assert result.source_key == "mgi"
    catalog = source_catalog("MGI:2", "MGI:1", "MGI:1")
    assert (result.source_url, result.source_checksum) == (
        catalog.source.source_url,
        catalog.source.source_checksum,
    )
    assert service.completed(job_id) == result.to_job_result()
    assert service.publish(job_id=job_id, actor_id="redelivery") == result
    service.stage(job_id=job_id, source_key="mgi", catalog=catalog)
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(EntitySourceRecord).order_by(EntitySourceRecord.line_number)
            )
        )
        assert [row.entity["db_object_symbol"] for row in rows] == [
            "SYM0",
            "SYM1",
            "SYM2",
        ]
        assert [row.line_number for row in rows] == [4, 5, 6]
        assert (
            session.scalar(select(func.count()).select_from(EntityStagingRecord)) == 0
        )
        assert session.scalar(select(func.count()).select_from(AuditEventRecord)) == 1
        assert (
            session.scalar(select(AuditEventRecord.action))
            == "entity.catalog_published"
        )


def test_replacement_reports_full_sorted_impacts_without_mutating_annotations(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Replacing a catalog reports sorted impacts and leaves annotations unchanged.

    Each impact lists the non-deleted annotations on a removed entity.
    """
    service = EntityImportService(unit_of_work_factory)
    old = stage(service, session_factory, "MGI:z", "MGI:a", "MGI:keep")
    old_result = service.publish(job_id=old, actor_id="curator")
    other = stage(service, session_factory, "RGD:1", source="rgd")
    service.publish(job_id=other, actor_id="curator")
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
            uow.annotations.create(
                annotation=annotation,
                actor_id="curator",
                change_source="test",
                owning_group_id="MGI",
                record_origin=AnnotationOrigin.DIRECT,
                annotation_id=annotation_id,
            )
        deleted = uow.annotations.soft_delete(
            UUID(int=4), actor_id="curator", change_source="test"
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
    new = stage(service, session_factory, "MGI:new", "MGI:keep")
    result = service.publish(job_id=new, actor_id="curator")
    assert (result.added_count, result.retained_count, result.removed_count) == (
        1,
        1,
        2,
    )
    assert result.to_job_result()["removal_impacts"] == [
        {
            "db_object_id": "MGI:a",
            "annotation_ids": [str(UUID(int=1)), str(UUID(int=2))],
        },
        {"db_object_id": "MGI:z", "annotation_ids": [str(UUID(int=3))]},
    ]
    assert active_ids(session_factory) == ["MGI:keep", "MGI:new", "RGD:1"]
    assert service.completed(old) == old_result.to_job_result()
    with session_factory() as session:
        assert list(session.execute(annotation_query).mappings()) == annotations_before
        assert list(session.execute(history_query).mappings()) == history_before
        assert (
            session.scalar(
                select(func.count()).select_from(EntityCatalogSnapshotRecord)
            )
            == 3
        )


def test_publish_empty_catalog_removes_only_that_source(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Publishing a catalog with no entities removes only that source's entities."""
    service = EntityImportService(unit_of_work_factory)
    for prefix, key in (("MGI", "mgi"), ("RGD", "rgd")):
        job_id = stage(service, session_factory, f"{prefix}:1", source=key)
        service.publish(job_id=job_id, actor_id="curator")
    empty = stage(service, session_factory)
    result = service.publish(job_id=empty, actor_id="curator")
    assert (
        result.source_record_count,
        result.active_identifier_count,
        result.removed_count,
    ) == (0, 0, 1)
    assert active_ids(session_factory) == ["RGD:1"]


def test_collision_rolls_back_complete_catalog_replacement(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """An identifier already active in another source blocks publication.

    Both sources keep their active catalogs and the staged rows remain for a retry.
    """
    service = EntityImportService(unit_of_work_factory)
    for prefix, key in (("MGI", "mgi"), ("RGD", "rgd")):
        job_id = stage(service, session_factory, f"{prefix}:old", source=key)
        service.publish(job_id=job_id, actor_id="curator")
    candidate = stage(service, session_factory, "MGI:new", "RGD:old")
    with pytest.raises(EntityCatalogCollisionError) as caught:
        service.publish(job_id=candidate, actor_id="curator")
    assert caught.value.colliding_ids == ("RGD:old",)
    assert active_ids(session_factory) == ["MGI:old", "RGD:old"]
    assert service.completed(candidate) is None


@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_failure_after_publication_restores_old_state(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A failure while recording the audit event or committing keeps the old catalog.

    No result or new audit event is stored.
    """
    service = EntityImportService(unit_of_work_factory)
    old = stage(service, session_factory, "MGI:old")
    service.publish(job_id=old, actor_id="curator")
    candidate = stage(service, session_factory, "MGI:new")

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected failure")

    with monkeypatch.context() as patch:
        if failure == "audit":
            patch.setattr(AuditService, "record_entity_catalog_published", fail)
        else:
            patch.setattr(Session, "commit", fail)
        with pytest.raises(RuntimeError, match="injected failure"):
            service.publish(job_id=candidate, actor_id="curator")
    assert active_ids(session_factory) == ["MGI:old"]
    assert service.completed(candidate) is None
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AuditEventRecord)) == 1
        assert (
            session.scalar(select(func.count()).select_from(EntityStagingRecord)) == 1
        )


def test_stale_candidate_cannot_replace_a_newer_publication(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Publishing an older staged catalog after a newer one raises a conflict."""
    service = EntityImportService(unit_of_work_factory)
    old = stage(service, session_factory, "MGI:old")
    new = stage(service, session_factory, "MGI:new")
    service.publish(job_id=new, actor_id="curator")
    with pytest.raises(EntityCandidateConflictError):
        service.publish(job_id=old, actor_id="curator")
    assert active_ids(session_factory) == ["MGI:new"]


def test_publication_spans_multiple_insert_batches_without_losing_records(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A catalog larger than one insert batch is published with every record."""
    service = EntityImportService(unit_of_work_factory)
    job_id = stage(service, session_factory, *(f"MGI:{index}" for index in range(5001)))
    result = service.publish(job_id=job_id, actor_id="curator")
    assert result.source_record_count == result.active_identifier_count == 5001
    assert len(active_ids(session_factory)) == 5001
