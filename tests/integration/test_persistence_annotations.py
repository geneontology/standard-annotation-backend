"""Tests for storing, changing, and deleting annotations in PostgreSQL."""

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence.annotation_data import (
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.locks import acquire_signature_locks
from standard_annotation_backend.persistence.models import (
    AnnotationDuplicateReferenceRecord,
    AnnotationMultivaluedFieldValueRecord,
    AnnotationOrigin,
    AnnotationRecord,
    AnnotationStatus,
    AnnotationVersionRecord,
    JobRecord,
)
from standard_annotation_backend.persistence.repositories import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
    AnnotationRepository,
    InvalidAnnotationProvenanceError,
)
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork

UnitOfWorkFactory = Callable[[], SqlAlchemyUnitOfWork]


def _changed_annotation(annotation: Annotation, **changes: object) -> Annotation:
    annotation_data = annotation.model_dump(mode="json")
    annotation_data.update(changes)
    return Annotation.model_validate(annotation_data)


def _create_direct(
    unit_of_work_factory: UnitOfWorkFactory,
    annotation: Annotation,
    *,
    annotation_id: UUID | None = None,
) -> UUID:
    with unit_of_work_factory() as unit_of_work:
        record = unit_of_work.annotations.create(
            annotation=annotation,
            actor_id="creator",
            change_source="api",
            owning_group_id="group-1",
            record_origin=AnnotationOrigin.DIRECT,
            annotation_id=annotation_id,
        )
        unit_of_work.commit()
        return record.annotation_id


def _create_import_job(session_factory: sessionmaker[Session]) -> UUID:
    job_id = uuid4()
    with session_factory.begin() as session:
        session.add(
            JobRecord(
                job_id=job_id,
                job_type="import",
                status="complete",
                requested_by="importer",
                parameters={},
            )
        )
    return job_id


def test_direct_create_stores_current_version_and_derived_values(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    persistence_data = prepare_annotation_for_persistence(validated_annotation)

    annotation_id = _create_direct(unit_of_work_factory, validated_annotation)

    with session_factory() as session:
        current = session.get(AnnotationRecord, annotation_id)
        versions = session.scalars(
            select(AnnotationVersionRecord).where(
                AnnotationVersionRecord.annotation_id == annotation_id
            )
        ).all()
        field_rows = session.scalars(
            select(AnnotationMultivaluedFieldValueRecord)
            .where(AnnotationMultivaluedFieldValueRecord.annotation_id == annotation_id)
            .order_by(
                AnnotationMultivaluedFieldValueRecord.field_name,
                AnnotationMultivaluedFieldValueRecord.field_value,
            )
        ).all()
        reference_rows = session.scalars(
            select(AnnotationDuplicateReferenceRecord)
            .where(AnnotationDuplicateReferenceRecord.annotation_id == annotation_id)
            .order_by(AnnotationDuplicateReferenceRecord.canonical_reference)
        ).all()

    assert current is not None
    assert current.annotation_data == persistence_data.annotation_data
    assert current.current_version == 1
    assert current.status == AnnotationStatus.ACTIVE
    assert current.owning_group_id == "group-1"
    assert current.record_origin == AnnotationOrigin.DIRECT
    assert current.source_import_job_id is None
    assert current.duplicate_base_signature == persistence_data.duplicate_base_signature
    assert (
        current.db_object_id,
        current.negation,
        current.relation,
        current.ontology_class_id,
        current.evidence_type,
        current.annotation_date,
        current.assigned_by,
    ) == (
        persistence_data.db_object_id,
        persistence_data.negation,
        persistence_data.relation,
        persistence_data.ontology_class_id,
        persistence_data.evidence_type,
        persistence_data.annotation_date,
        persistence_data.assigned_by,
    )
    assert [(row.version, row.annotation_data, row.is_deleted) for row in versions] == [
        (1, persistence_data.annotation_data, False)
    ]
    assert [(row.field_name, row.field_value) for row in field_rows] == [
        (value.field_name, value.field_value)
        for value in persistence_data.multivalued_field_values
    ]
    assert [row.canonical_reference for row in reference_rows] == list(
        persistence_data.canonical_references
    )
    assert all(
        row.duplicate_base_signature == persistence_data.duplicate_base_signature
        for row in reference_rows
    )


def test_imported_duplicate_peers_coexist_and_find_each_other(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    job_id = _create_import_job(session_factory)
    first_id = uuid4()
    second_id = uuid4()
    persistence_data = prepare_annotation_for_persistence(validated_annotation)

    for annotation_id in (first_id, second_id):
        with unit_of_work_factory() as unit_of_work:
            unit_of_work.annotations.create(
                annotation=validated_annotation,
                actor_id="importer",
                change_source="legacy-import",
                owning_group_id="legacy",
                record_origin=AnnotationOrigin.IMPORT,
                source_import_job_id=job_id,
                annotation_id=annotation_id,
            )
            unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        assert unit_of_work.annotations.find_duplicate_peer_ids(
            persistence_data.duplicate_base_signature,
            persistence_data.canonical_references,
            exclude_annotation_id=first_id,
        ) == (second_id,)
        assert unit_of_work.annotations.find_duplicate_peer_ids(
            persistence_data.duplicate_base_signature,
            [persistence_data.canonical_references[0]],
            exclude_annotation_id=second_id,
        ) == (first_id,)


@pytest.mark.parametrize(
    ("record_origin", "has_import_job"),
    [
        (AnnotationOrigin.DIRECT, True),
        (AnnotationOrigin.IMPORT, False),
        ("unsupported", False),
    ],
)
def test_invalid_provenance_raises_before_flush(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    record_origin: str | AnnotationOrigin,
    has_import_job: bool,
) -> None:
    job_id = _create_import_job(session_factory) if has_import_job else None

    with (
        unit_of_work_factory() as unit_of_work,
        pytest.raises(InvalidAnnotationProvenanceError),
    ):
        unit_of_work.annotations.create(
            annotation=validated_annotation,
            actor_id="actor",
            change_source="test",
            owning_group_id="group-1",
            record_origin=record_origin,
            source_import_job_id=job_id,
        )

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AnnotationRecord)) == 0


def test_update_appends_version_and_replaces_current_derived_values(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_direct(unit_of_work_factory, validated_annotation)
    changed = _changed_annotation(
        validated_annotation,
        assigned_by="Updated_Source",
        references=["PMID:99"],
        with_or_from=["UniProtKB:NEW"],
        interacting_taxon_id=None,
    )
    changed_persistence_data = prepare_annotation_for_persistence(changed)

    with unit_of_work_factory() as unit_of_work:
        record = unit_of_work.annotations.update(
            annotation_id,
            changed,
            actor_id="editor",
            change_source="api",
        )
        unit_of_work.commit()
        assert record.current_version == 2

    with unit_of_work_factory() as unit_of_work:
        current = unit_of_work.annotations.get(annotation_id)
        versions = unit_of_work.annotations.list_versions(annotation_id)
        assert current is not None
        assert current.annotation_data == changed_persistence_data.annotation_data
        assert current.current_version == 2
        assert current.assigned_by == "Updated_Source"
        version_snapshots = tuple(
            (version.version, version.is_deleted, version.annotation_data)
            for version in versions
        )
    with session_factory() as session:
        field_rows = session.scalars(
            select(AnnotationMultivaluedFieldValueRecord)
            .where(AnnotationMultivaluedFieldValueRecord.annotation_id == annotation_id)
            .order_by(
                AnnotationMultivaluedFieldValueRecord.field_name,
                AnnotationMultivaluedFieldValueRecord.field_value,
            )
        ).all()
        reference_rows = session.scalars(
            select(AnnotationDuplicateReferenceRecord).where(
                AnnotationDuplicateReferenceRecord.annotation_id == annotation_id
            )
        ).all()

    assert version_snapshots == (
        (
            1,
            False,
            prepare_annotation_for_persistence(validated_annotation).annotation_data,
        ),
        (2, False, changed_persistence_data.annotation_data),
    )
    assert [(row.field_name, row.field_value) for row in field_rows] == [
        (value.field_name, value.field_value)
        for value in changed_persistence_data.multivalued_field_values
    ]
    assert [row.canonical_reference for row in reference_rows] == ["PMID:99"]


def test_update_locks_intervening_signature_for_retained_orm_object(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_direct(unit_of_work_factory, validated_annotation)
    original_signature = prepare_annotation_for_persistence(
        validated_annotation
    ).duplicate_base_signature
    intervening = _changed_annotation(
        validated_annotation,
        db_object_id="UniProtKB:INTERVENING",
    )
    intervening_signature = prepare_annotation_for_persistence(
        intervening
    ).duplicate_base_signature
    candidate = _changed_annotation(
        validated_annotation,
        db_object_id="UniProtKB:CANDIDATE",
    )

    with session_factory() as retained_session:
        retained = retained_session.get(AnnotationRecord, annotation_id)
        assert retained is not None
        assert retained.duplicate_base_signature == original_signature

        with unit_of_work_factory() as unit_of_work:
            unit_of_work.annotations.update(
                annotation_id,
                intervening,
                actor_id="intervening-editor",
                change_source="api",
            )
            unit_of_work.commit()

        assert retained.duplicate_base_signature == original_signature
        retained_session.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with session_factory() as blocker_session:
            acquire_signature_locks(blocker_session, [intervening_signature])

            with pytest.raises(DBAPIError, match="lock timeout"):
                AnnotationRepository(retained_session).update(
                    annotation_id,
                    candidate,
                    actor_id="final-editor",
                    change_source="api",
                )


def test_soft_delete_locks_intervening_signature_for_retained_orm_object(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_direct(unit_of_work_factory, validated_annotation)
    original_signature = prepare_annotation_for_persistence(
        validated_annotation
    ).duplicate_base_signature
    intervening = _changed_annotation(
        validated_annotation,
        db_object_id="UniProtKB:INTERVENING",
    )
    intervening_signature = prepare_annotation_for_persistence(
        intervening
    ).duplicate_base_signature

    with session_factory() as retained_session:
        retained = retained_session.get(AnnotationRecord, annotation_id)
        assert retained is not None
        assert retained.duplicate_base_signature == original_signature

        with unit_of_work_factory() as unit_of_work:
            unit_of_work.annotations.update(
                annotation_id,
                intervening,
                actor_id="intervening-editor",
                change_source="api",
            )
            unit_of_work.commit()

        assert retained.duplicate_base_signature == original_signature
        retained_session.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with session_factory() as blocker_session:
            acquire_signature_locks(blocker_session, [intervening_signature])

            with pytest.raises(DBAPIError, match="lock timeout"):
                AnnotationRepository(retained_session).soft_delete(
                    annotation_id,
                    actor_id="deleter",
                    change_source="api",
                )


def test_soft_delete_hides_current_retains_history_and_clears_derived_values(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_direct(unit_of_work_factory, validated_annotation)

    with unit_of_work_factory() as unit_of_work:
        deleted = unit_of_work.annotations.soft_delete(
            annotation_id,
            actor_id="deleter",
            change_source="api",
        )
        unit_of_work.commit()
        assert deleted.current_version == 2
        assert deleted.status == AnnotationStatus.DELETED
        assert deleted.deleted_at is not None

    with unit_of_work_factory() as unit_of_work:
        assert unit_of_work.annotations.get(annotation_id) is None
        retained = unit_of_work.annotations.get(annotation_id, include_deleted=True)
        versions = unit_of_work.annotations.list_versions(annotation_id)
        assert retained is not None
        assert retained.status == AnnotationStatus.DELETED
        version_snapshots = tuple(
            (version.version, version.is_deleted, version.annotation_data)
            for version in versions
        )
    with session_factory() as session:
        field_count = session.scalar(
            select(func.count())
            .select_from(AnnotationMultivaluedFieldValueRecord)
            .where(AnnotationMultivaluedFieldValueRecord.annotation_id == annotation_id)
        )
        reference_count = session.scalar(
            select(func.count())
            .select_from(AnnotationDuplicateReferenceRecord)
            .where(AnnotationDuplicateReferenceRecord.annotation_id == annotation_id)
        )

    assert [(version, is_deleted) for version, is_deleted, _ in version_snapshots] == [
        (1, False),
        (2, True),
    ]
    assert version_snapshots[1][2] == version_snapshots[0][2]
    assert field_count == 0
    assert reference_count == 0


def test_missing_and_deleted_write_transitions_raise_focused_errors(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_direct(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        unit_of_work.annotations.soft_delete(
            annotation_id,
            actor_id="deleter",
            change_source="api",
        )
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        with pytest.raises(AnnotationDeletedError):
            unit_of_work.annotations.update(
                annotation_id,
                validated_annotation,
                actor_id="editor",
                change_source="api",
            )
        with pytest.raises(AnnotationDeletedError):
            unit_of_work.annotations.soft_delete(
                annotation_id,
                actor_id="deleter",
                change_source="api",
            )

    missing_id = uuid4()
    with unit_of_work_factory() as unit_of_work:
        with pytest.raises(AnnotationNotFoundError):
            unit_of_work.annotations.update(
                missing_id,
                validated_annotation,
                actor_id="editor",
                change_source="api",
            )
        with pytest.raises(AnnotationNotFoundError):
            unit_of_work.annotations.soft_delete(
                missing_id,
                actor_id="deleter",
                change_source="api",
            )


def test_unit_of_work_rolls_back_without_explicit_commit(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = uuid4()

    with unit_of_work_factory() as unit_of_work:
        unit_of_work.annotations.create(
            annotation=validated_annotation,
            actor_id="creator",
            change_source="api",
            owning_group_id="group-1",
            record_origin=AnnotationOrigin.DIRECT,
            annotation_id=annotation_id,
        )

    with session_factory() as session:
        assert session.get(AnnotationRecord, annotation_id) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(AnnotationVersionRecord)
                .where(AnnotationVersionRecord.annotation_id == annotation_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AnnotationMultivaluedFieldValueRecord)
                .where(
                    AnnotationMultivaluedFieldValueRecord.annotation_id == annotation_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AnnotationDuplicateReferenceRecord)
                .where(
                    AnnotationDuplicateReferenceRecord.annotation_id == annotation_id
                )
            )
            == 0
        )
