"""Test database rules for annotations created and changed through the API."""

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence import repositories
from standard_annotation_backend.persistence.models import AnnotationOrigin, JobRecord
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork

UnitOfWorkFactory = Callable[[], SqlAlchemyUnitOfWork]


def _changed(annotation: Annotation, **changes: object) -> Annotation:
    annotation_data = annotation.model_dump(mode="json")
    annotation_data.update(changes)
    return Annotation.model_validate(annotation_data)


def _create_existing(
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


def _create_legacy_annotations(
    unit_of_work_factory: UnitOfWorkFactory,
    annotations: tuple[tuple[UUID, Annotation], ...],
) -> None:
    job_id = uuid4()
    with unit_of_work_factory() as unit_of_work:
        import_job = JobRecord(
            job_id=job_id,
            job_type="import",
            status="complete",
            requested_by="importer",
            parameters={},
        )
        unit_of_work.annotations.session.add(import_job)
        unit_of_work.annotations.session.flush([import_job])
        for annotation_id, annotation in annotations:
            unit_of_work.annotations.create(
                annotation=annotation,
                actor_id="importer",
                change_source="legacy-import",
                owning_group_id="legacy",
                record_origin=AnnotationOrigin.IMPORT,
                source_import_job_id=job_id,
                annotation_id=annotation_id,
            )
        unit_of_work.commit()


def test_create_direct_rejects_an_active_duplicate(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    existing_id = _create_existing(unit_of_work_factory, validated_annotation)

    with (
        unit_of_work_factory() as unit_of_work,
        pytest.raises(repositories.DuplicateAnnotationError) as raised,
    ):
        unit_of_work.annotations.create_direct(
            annotation=validated_annotation,
            actor_id="api-user",
            owning_group_id="group-1",
        )

    assert raised.value.peer_ids == (existing_id,)


def test_update_direct_rejects_a_stale_expected_version(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_existing(unit_of_work_factory, validated_annotation)
    changed_annotation = _changed(validated_annotation, assigned_by="MGI")

    with (
        unit_of_work_factory() as unit_of_work,
        pytest.raises(repositories.StaleAnnotationVersionError) as raised,
    ):
        unit_of_work.annotations.update_direct(
            annotation_id,
            changed_annotation,
            expected_version=2,
            actor_id="api-user",
        )

    assert raised.value.current_version == 1


def test_update_direct_rejects_only_a_new_duplicate_peer(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    first_id = UUID("00000000-0000-0000-0000-000000000001")
    second_id = UUID("00000000-0000-0000-0000-000000000002")
    candidate_id = UUID("00000000-0000-0000-0000-000000000003")
    _create_legacy_annotations(
        unit_of_work_factory,
        (
            (first_id, validated_annotation),
            (second_id, validated_annotation),
        ),
    )
    _create_existing(
        unit_of_work_factory,
        _changed(validated_annotation, db_object_id="UniProtKB:DISTINCT"),
        annotation_id=candidate_id,
    )

    with unit_of_work_factory() as unit_of_work:
        preserved = unit_of_work.annotations.update_direct(
            first_id,
            _changed(validated_annotation, assigned_by="MGI"),
            expected_version=1,
            actor_id="api-user",
        )
        unit_of_work.commit()
    assert preserved.current_version == 2

    with (
        unit_of_work_factory() as unit_of_work,
        pytest.raises(repositories.DuplicateAnnotationError) as raised,
    ):
        unit_of_work.annotations.update_direct(
            candidate_id,
            validated_annotation,
            expected_version=1,
            actor_id="api-user",
        )
    assert raised.value.peer_ids == (first_id, second_id)


def test_update_direct_rejects_expansion_of_a_legacy_peer_set(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    first_id = UUID("00000000-0000-0000-0000-000000000101")
    existing_peer_id = UUID("00000000-0000-0000-0000-000000000102")
    new_peer_id = UUID("00000000-0000-0000-0000-000000000103")
    original = _changed(validated_annotation, references=["PMID:1"])
    _create_legacy_annotations(
        unit_of_work_factory,
        (
            (first_id, original),
            (existing_peer_id, original),
            (
                new_peer_id,
                _changed(validated_annotation, references=["PMID:2"]),
            ),
        ),
    )

    with (
        unit_of_work_factory() as unit_of_work,
        pytest.raises(repositories.DuplicateAnnotationError) as raised,
    ):
        unit_of_work.annotations.update_direct(
            first_id,
            _changed(original, references=["PMID:1", "PMID:2"]),
            expected_version=1,
            actor_id="api-user",
        )

    assert raised.value.peer_ids == (new_peer_id,)
    with unit_of_work_factory() as unit_of_work:
        retained = unit_of_work.annotations.get(first_id)
        versions = unit_of_work.annotations.list_versions(first_id)
        assert retained is not None
        assert retained.current_version == 1
        assert retained.annotation_data["references"] == ["PMID:1"]
        assert [(version.version, version.is_deleted) for version in versions] == [
            (1, False)
        ]


def test_soft_delete_direct_requires_the_current_version(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_existing(unit_of_work_factory, validated_annotation)

    with (
        unit_of_work_factory() as unit_of_work,
        pytest.raises(repositories.StaleAnnotationVersionError),
    ):
        unit_of_work.annotations.soft_delete_direct(
            annotation_id,
            expected_version=2,
            actor_id="api-user",
        )


def test_soft_delete_direct_appends_the_deleted_version(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_existing(unit_of_work_factory, validated_annotation)

    with unit_of_work_factory() as unit_of_work:
        deleted = unit_of_work.annotations.soft_delete_direct(
            annotation_id,
            expected_version=1,
            actor_id="api-user",
        )
        unit_of_work.commit()

    assert deleted.current_version == 2
    with unit_of_work_factory() as unit_of_work:
        versions = unit_of_work.annotations.list_versions(annotation_id)
        version_states = tuple(
            (version.version, version.is_deleted) for version in versions
        )
    assert version_states == (
        (1, False),
        (2, True),
    )


def test_soft_delete_direct_rejects_an_already_deleted_annotation(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_existing(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        unit_of_work.annotations.soft_delete_direct(
            annotation_id,
            expected_version=1,
            actor_id="api-user",
        )
        unit_of_work.commit()

    with (
        unit_of_work_factory() as unit_of_work,
        pytest.raises(repositories.AnnotationDeletedError),
    ):
        unit_of_work.annotations.soft_delete_direct(
            annotation_id,
            expected_version=2,
            actor_id="api-user",
        )
