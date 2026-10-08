"""Tests for storing, changing, and deleting annotations in PostgreSQL."""

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from seeding import insert_annotation
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotation_search import (
    AnnotationFilter,
    OwnershipScope,
)
from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationDeletedError,
    AnnotationNotFoundError,
    AnnotationOrigin,
    AnnotationStatus,
    ChangeSource,
)
from standard_annotation_backend.persistence.annotation_data import (
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.locks import acquire_signature_locks
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    JobRecord,
)
from standard_annotation_backend.persistence.repositories import (
    AnnotationRepository,
)
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork

UnitOfWorkFactory = Callable[[], SqlAlchemyUnitOfWork]


def _changed_annotation(annotation: Annotation, **changes: object) -> Annotation:
    annotation_data = annotation.model_dump(mode="json")
    annotation_data.update(changes)
    return Annotation.model_validate(annotation_data)


@pytest.fixture(autouse=True)
def active_subjects(seed_active_subjects: Callable[..., None]) -> None:
    """Make the subjects these tests create and update active entities."""
    seed_active_subjects(
        "UniProtKB:P12345", "UniProtKB:INTERVENING", "UniProtKB:CANDIDATE"
    )


def _create_annotation(
    unit_of_work_factory: UnitOfWorkFactory,
    annotation: Annotation,
) -> UUID:
    with unit_of_work_factory() as unit_of_work:
        record = unit_of_work.annotations.create(
            annotation=annotation,
            actor_id="creator",
            owning_group_id="group-1",
        )
        unit_of_work.commit()
        return record.annotation_id


def _create_source_job(session_factory: sessionmaker[Session]) -> UUID:
    job_id = uuid4()
    with session_factory.begin() as session:
        session.add(
            JobRecord(
                job_id=job_id,
                job_type="authorization_refresh",
                status="queued",
                requested_by="importer",
                parameters={},
            )
        )
    return job_id


def test_direct_create_stores_current_version_and_derived_values(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """A created annotation is readable, versioned, searchable, and a duplicate peer."""
    persistence_data = prepare_annotation_for_persistence(validated_annotation)

    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)

    with unit_of_work_factory() as unit_of_work:
        repository = unit_of_work.annotations
        current = repository.get(annotation_id)
        versions = repository.list_versions_page(annotation_id, limit=100, offset=0)
        assert current is not None
        assert current.current_version == 1
        assert current.status == AnnotationStatus.ACTIVE
        assert current.owning_group_id == "group-1"
        assert current.record_origin == AnnotationOrigin.DIRECT
        assert current.source_import_job_id is None
        assert current.annotation_data == validated_annotation.model_dump(mode="json")
        assert [(v.version, v.is_deleted) for v in versions.items] == [(1, False)]
        for criteria in (
            AnnotationFilter(references=("PMID:1",)),
            AnnotationFilter(with_or_from=("UniProtKB:Q1",)),
            AnnotationFilter(interacting_taxon_id=("NCBITaxon:9606",)),
        ):
            found = repository.list_active(
                criteria, OwnershipScope(), limit=10, offset=0
            )
            assert [record.annotation_id for record in found.items] == [annotation_id]
        assert repository.find_duplicate_peer_ids(
            persistence_data.duplicate_base_signature,
            [persistence_data.canonical_references[0]],
        ) == (annotation_id,)


def test_imported_duplicate_peers_coexist_and_find_each_other(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    job_id = _create_source_job(session_factory)
    first_id = uuid4()
    second_id = uuid4()
    persistence_data = prepare_annotation_for_persistence(validated_annotation)

    for annotation_id in (first_id, second_id):
        with unit_of_work_factory() as unit_of_work:
            insert_annotation(
                unit_of_work.annotations.session,
                annotation=validated_annotation,
                actor_id="importer",
                change_source=ChangeSource.ANNOTATION_REFRESH,
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


def test_update_appends_version_and_replaces_current_derived_values(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """An update appends a version and makes search and duplicate checks use new values."""
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
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
            expected_version=1,
            actor_id="editor",
            change_source=ChangeSource.API,
        )
        unit_of_work.commit()
        assert record.current_version == 2

    with unit_of_work_factory() as unit_of_work:
        repository = unit_of_work.annotations
        current = repository.get(annotation_id)
        versions = repository.list_versions_page(
            annotation_id, limit=100, offset=0
        ).items
        assert current is not None
        assert current.annotation_data == changed_persistence_data.annotation_data
        assert current.current_version == 2
        assert current.assigned_by == "Updated_Source"
        version_snapshots = tuple(
            (version.version, version.is_deleted, version.annotation_data)
            for version in versions
        )

        def matching_ids(criteria: AnnotationFilter) -> list[UUID]:
            page = repository.list_active(
                criteria, OwnershipScope(), limit=10, offset=0
            )
            return [record.annotation_id for record in page.items]

        assert matching_ids(AnnotationFilter(references=("PMID:99",))) == [
            annotation_id
        ]
        assert matching_ids(AnnotationFilter(with_or_from=("UniProtKB:NEW",))) == [
            annotation_id
        ]
        assert matching_ids(AnnotationFilter(references=("PMID:1",))) == []
        assert matching_ids(AnnotationFilter(with_or_from=("UniProtKB:Q1",))) == []
        assert (
            matching_ids(AnnotationFilter(interacting_taxon_id=("NCBITaxon:9606",)))
            == []
        )
        assert repository.find_duplicate_peer_ids(
            changed_persistence_data.duplicate_base_signature, ["PMID:99"]
        ) == (annotation_id,)

    assert version_snapshots == (
        (
            1,
            False,
            prepare_annotation_for_persistence(validated_annotation).annotation_data,
        ),
        (2, False, changed_persistence_data.annotation_data),
    )


def test_update_locks_intervening_signature_for_retained_orm_object(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
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
                expected_version=1,
                actor_id="intervening-editor",
                change_source=ChangeSource.API,
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
                    expected_version=2,
                    actor_id="final-editor",
                    change_source=ChangeSource.API,
                )


def test_soft_delete_locks_intervening_signature_for_retained_orm_object(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
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
                expected_version=1,
                actor_id="intervening-editor",
                change_source=ChangeSource.API,
            )
            unit_of_work.commit()

        assert retained.duplicate_base_signature == original_signature
        retained_session.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with session_factory() as blocker_session:
            acquire_signature_locks(blocker_session, [intervening_signature])

            with pytest.raises(DBAPIError, match="lock timeout"):
                AnnotationRepository(retained_session).soft_delete(
                    annotation_id,
                    expected_version=2,
                    actor_id="deleter",
                    change_source=ChangeSource.API,
                )


def test_soft_delete_hides_current_retains_history_and_clears_derived_values(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """A deleted annotation leaves search and duplicate checks but keeps its history."""
    persistence_data = prepare_annotation_for_persistence(validated_annotation)
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)

    with unit_of_work_factory() as unit_of_work:
        deleted = unit_of_work.annotations.soft_delete(
            annotation_id,
            expected_version=1,
            actor_id="deleter",
            change_source=ChangeSource.API,
        )
        unit_of_work.commit()
        assert deleted.current_version == 2
        assert deleted.status == AnnotationStatus.DELETED
        assert deleted.deleted_at is not None

    with unit_of_work_factory() as unit_of_work:
        repository = unit_of_work.annotations
        assert repository.get(annotation_id) is None
        retained = repository.get(annotation_id, include_deleted=True)
        versions = repository.list_versions_page(
            annotation_id, limit=100, offset=0
        ).items
        assert retained is not None
        assert retained.status == AnnotationStatus.DELETED
        version_snapshots = tuple(
            (version.version, version.is_deleted, version.annotation_data)
            for version in versions
        )
        found = repository.list_active(
            AnnotationFilter(references=("PMID:1",)),
            OwnershipScope(),
            limit=10,
            offset=0,
        )
        assert found.total == 0
        assert (
            repository.find_duplicate_peer_ids(
                persistence_data.duplicate_base_signature,
                persistence_data.canonical_references,
            )
            == ()
        )

    assert [(version, is_deleted) for version, is_deleted, _ in version_snapshots] == [
        (1, False),
        (2, True),
    ]
    assert version_snapshots[1][2] == version_snapshots[0][2]
    # The identical annotation can be created again, so it no longer counts as a duplicate.
    assert (
        _create_annotation(unit_of_work_factory, validated_annotation) != annotation_id
    )


def test_missing_and_deleted_write_transitions_raise_focused_errors(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        unit_of_work.annotations.soft_delete(
            annotation_id,
            expected_version=1,
            actor_id="deleter",
            change_source=ChangeSource.API,
        )
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        with pytest.raises(AnnotationDeletedError):
            unit_of_work.annotations.update(
                annotation_id,
                validated_annotation,
                expected_version=2,
                actor_id="editor",
                change_source=ChangeSource.API,
            )
        with pytest.raises(AnnotationDeletedError):
            unit_of_work.annotations.soft_delete(
                annotation_id,
                expected_version=2,
                actor_id="deleter",
                change_source=ChangeSource.API,
            )

    missing_id = uuid4()
    with unit_of_work_factory() as unit_of_work:
        with pytest.raises(AnnotationNotFoundError):
            unit_of_work.annotations.update(
                missing_id,
                validated_annotation,
                expected_version=1,
                actor_id="editor",
                change_source=ChangeSource.API,
            )
        with pytest.raises(AnnotationNotFoundError):
            unit_of_work.annotations.soft_delete(
                missing_id,
                expected_version=1,
                actor_id="deleter",
                change_source=ChangeSource.API,
            )


def test_unit_of_work_rolls_back_without_explicit_commit(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """Changes made in a unit of work that is never committed are not saved."""
    with unit_of_work_factory() as unit_of_work:
        annotation_id = unit_of_work.annotations.create(
            annotation=validated_annotation,
            actor_id="creator",
            owning_group_id="group-1",
        ).annotation_id

    with unit_of_work_factory() as unit_of_work:
        repository = unit_of_work.annotations
        assert repository.get(annotation_id, include_deleted=True) is None
        assert (
            repository.list_versions_page(annotation_id, limit=100, offset=0).total == 0
        )
