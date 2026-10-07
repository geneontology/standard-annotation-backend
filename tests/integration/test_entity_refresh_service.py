"""Verify staging, publication, and replacement of entity catalogs in PostgreSQL."""

from uuid import UUID

import pytest
from refresh_helpers import (
    TEST_SOURCES,
    ignore_progress,
    source_document,
    stage_without_publishing,
    start_job,
)
from seeding import insert_annotation
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    ChangeSource,
)
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.entities import (
    EntityCandidateConflictError,
    EntityCatalogCollisionError,
    EntityRefreshResult,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import SourceDocument
from standard_annotation_backend.gpi.parser import parse_gpi
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationVersionRecord,
    AuditEventRecord,
    EntityCatalogSnapshotRecord,
    EntityMembershipRecord,
    EntitySourceRecord,
    EntityStagingRecord,
)
from standard_annotation_backend.persistence.repositories.entities import (
    EntityRepository,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)
from standard_annotation_backend.services.job_service import Job


def source_gpi(*identifiers: str) -> str:
    """Return valid GPI text, preserving repeated identifiers and source order."""
    text = "!gpi-version: 2.0\n!generated-by: TestDB\n!date-generated: 2026-09-29\n"
    return text + "".join(
        f"{identifier}\tSYM{index}\tProtein\t\tSO:0001217\tNCBITaxon:9606\t\t{identifier}\t\t\t\n"
        for index, identifier in enumerate(identifiers)
    )


def _document(source: str, *identifiers: str) -> SourceDocument:
    return source_document(source, source_gpi(*identifiers).encode())


def stage_catalog(
    service: EntityRefreshService,
    factory: UnitOfWorkFactory,
    *identifiers: str,
    source: str = "mgi",
) -> Job:
    """Start a job and commit staging for `identifiers` without publishing."""
    job = start_job(factory, JobType.ENTITY_REFRESH, source)
    stage_without_publishing(
        service, job, _document(source, *identifiers), (EntityRepository, "publish")
    )
    return job


def publish_catalog(
    service: EntityRefreshService,
    factory: UnitOfWorkFactory,
    *identifiers: str,
    source: str = "mgi",
) -> Job:
    """Start a job and publish a catalog of `identifiers` for `source`."""
    job = start_job(factory, JobType.ENTITY_REFRESH, source)
    service.apply(job, _document(source, *identifiers), ignore_progress)
    return job


def stored_result(factory: UnitOfWorkFactory, job: Job) -> dict[str, object] | None:
    """Return the publication result stored for `job`, or `None` if unpublished."""
    with factory() as uow:
        return uow.entities.completed(job.job_id)


PUBLICATION_AUDIT_COUNT = (
    select(func.count())
    .select_from(AuditEventRecord)
    .where(AuditEventRecord.action == AuditAction.ENTITY_REFRESHED)
)


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

    Each distinct identifier becomes one active entity. Recovering the job or
    staging the same catalog again afterward returns the same result without
    adding rows or audit events.
    """
    service = EntityRefreshService(unit_of_work_factory, TEST_SOURCES)
    job = publish_catalog(service, unit_of_work_factory, "MGI:2", "MGI:1", "MGI:1")
    recovered = service.recover(job)
    assert recovered is not None
    result = EntityRefreshResult.from_job_result(recovered.result)
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
    document = _document("mgi", "MGI:2", "MGI:1", "MGI:1")
    assert (
        result.source_type,
        result.source_locator,
        result.source_revision,
        result.source_checksum,
    ) == (
        document.source_type,
        document.source_locator,
        document.source_revision,
        document.source_checksum,
    )
    assert stored_result(unit_of_work_factory, job) == recovered.result
    assert service.recover(job) == recovered
    catalog = parse_gpi(document.content.decode(), document.provenance)
    with unit_of_work_factory() as uow:
        uow.entities.stage(job.job_id, "mgi", catalog)
        uow.commit()
    assert service.recover(job) == recovered
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
        assert session.scalar(PUBLICATION_AUDIT_COUNT) == 1


def test_replacement_reports_full_sorted_impacts_without_mutating_annotations(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Replacing a catalog reports sorted impacts and leaves annotations unchanged.

    Each impact lists the non-deleted annotations on a removed entity.
    """
    service = EntityRefreshService(unit_of_work_factory, TEST_SOURCES)
    old = publish_catalog(service, unit_of_work_factory, "MGI:z", "MGI:a", "MGI:keep")
    old_result = stored_result(unit_of_work_factory, old)
    assert old_result is not None
    publish_catalog(service, unit_of_work_factory, "RGD:1", source="rgd")
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
    outcome = service.apply(
        new, _document("mgi", "MGI:new", "MGI:keep"), ignore_progress
    )
    result = EntityRefreshResult.from_job_result(outcome.result)
    assert (result.added_count, result.retained_count, result.removed_count) == (
        1,
        1,
        2,
    )
    assert outcome.result["removal_impacts"] == [
        {
            "db_object_id": "MGI:a",
            "annotation_ids": [str(UUID(int=1)), str(UUID(int=2))],
        },
        {"db_object_id": "MGI:z", "annotation_ids": [str(UUID(int=3))]},
    ]
    assert active_ids(session_factory) == ["MGI:keep", "MGI:new", "RGD:1"]
    assert stored_result(unit_of_work_factory, old) == old_result
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
    service = EntityRefreshService(unit_of_work_factory, TEST_SOURCES)
    for prefix, key in (("MGI", "mgi"), ("RGD", "rgd")):
        publish_catalog(service, unit_of_work_factory, f"{prefix}:1", source=key)
    empty = start_job(unit_of_work_factory, JobType.ENTITY_REFRESH, "mgi")
    outcome = service.apply(empty, _document("mgi"), ignore_progress)
    result = EntityRefreshResult.from_job_result(outcome.result)
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
    service = EntityRefreshService(unit_of_work_factory, TEST_SOURCES)
    for prefix, key in (("MGI", "mgi"), ("RGD", "rgd")):
        publish_catalog(service, unit_of_work_factory, f"{prefix}:old", source=key)
    candidate = start_job(unit_of_work_factory, JobType.ENTITY_REFRESH, "mgi")
    with pytest.raises(EntityCatalogCollisionError) as caught:
        service.apply(
            candidate, _document("mgi", "MGI:new", "RGD:old"), ignore_progress
        )
    assert caught.value.colliding_ids == ("RGD:old",)
    assert active_ids(session_factory) == ["MGI:old", "RGD:old"]
    assert stored_result(unit_of_work_factory, candidate) is None
    with session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(EntityStagingRecord)) == 2
        )


@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_failure_after_publication_restores_old_state(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A failure while recording the audit event or committing keeps the old catalog.

    No result or new audit event is stored, and the committed staging remains.
    """
    service = EntityRefreshService(unit_of_work_factory, TEST_SOURCES)
    publish_catalog(service, unit_of_work_factory, "MGI:old")
    candidate = stage_catalog(service, unit_of_work_factory, "MGI:new")
    audit_count = select(func.count()).select_from(AuditEventRecord)
    with session_factory() as session:
        audits_before = session.scalar(audit_count)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected failure")

    with monkeypatch.context() as patch:
        if failure == "audit":
            patch.setattr(AuditService, "record_entity_refreshed", fail)
        else:
            patch.setattr(Session, "commit", fail)
        with pytest.raises(RuntimeError, match="injected failure"):
            service.recover(candidate)
    assert active_ids(session_factory) == ["MGI:old"]
    assert stored_result(unit_of_work_factory, candidate) is None
    with session_factory() as session:
        assert session.scalar(audit_count) == audits_before
        assert session.scalar(PUBLICATION_AUDIT_COUNT) == 1
        assert (
            session.scalar(select(func.count()).select_from(EntityStagingRecord)) == 1
        )


def test_stale_candidate_cannot_replace_a_newer_publication(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """Publishing an older staged catalog after a newer one raises a conflict."""
    service = EntityRefreshService(unit_of_work_factory, TEST_SOURCES)
    old = stage_catalog(service, unit_of_work_factory, "MGI:old")
    new = stage_catalog(service, unit_of_work_factory, "MGI:new")
    with unit_of_work_factory() as uow:
        uow.entities.publish(new.job_id)
        uow.commit()
    with unit_of_work_factory() as uow, pytest.raises(EntityCandidateConflictError):
        uow.entities.publish(old.job_id)
    assert active_ids(session_factory) == ["MGI:new"]


def test_publication_spans_multiple_insert_batches_without_losing_records(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
) -> None:
    """A catalog larger than one insert batch is published with every record."""
    service = EntityRefreshService(unit_of_work_factory, TEST_SOURCES)
    job = publish_catalog(
        service, unit_of_work_factory, *(f"MGI:{index}" for index in range(5001))
    )
    result = stored_result(unit_of_work_factory, job)
    assert result is not None
    assert result["source_record_count"] == result["active_identifier_count"] == 5001
    assert len(active_ids(session_factory)) == 5001
