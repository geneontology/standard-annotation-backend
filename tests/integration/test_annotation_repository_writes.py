"""Test the database rules enforced by the annotation repository's write methods."""

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from seeding import insert_annotation

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    ChangeSource,
)
from standard_annotation_backend.persistence.models import (
    JobRecord,
)
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork

UnitOfWorkFactory = Callable[[], SqlAlchemyUnitOfWork]


def _changed(annotation: Annotation, **changes: object) -> Annotation:
    annotation_data = annotation.model_dump(mode="json")
    annotation_data.update(changes)
    return Annotation.model_validate(annotation_data)


def _create_legacy_annotations(
    unit_of_work_factory: UnitOfWorkFactory,
    annotations: tuple[tuple[UUID, Annotation], ...],
) -> None:
    job_id = uuid4()
    with unit_of_work_factory() as unit_of_work:
        source_job = JobRecord(
            job_id=job_id,
            job_type="authorization_refresh",
            status="queued",
            requested_by="importer",
            parameters={},
        )
        unit_of_work.annotations.session.add(source_job)
        unit_of_work.annotations.session.flush([source_job])
        for annotation_id, annotation in annotations:
            insert_annotation(
                unit_of_work.annotations.session,
                annotation=annotation,
                actor_id="importer",
                change_source=ChangeSource.ANNOTATION_REFRESH,
                owning_group_id="legacy",
                record_origin=AnnotationOrigin.IMPORT,
                source_import_job_id=job_id,
                annotation_id=annotation_id,
            )
        unit_of_work.commit()


def test_prepare_reports_every_active_peer_and_excludes_the_target(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """Prepared writes list all active duplicates, never the annotation itself.

    An update of one of two imported duplicates reports the other; a create of the
    same annotation reports both.
    """
    first_id = UUID("00000000-0000-0000-0000-000000000001")
    second_id = UUID("00000000-0000-0000-0000-000000000002")
    _create_legacy_annotations(
        unit_of_work_factory,
        ((first_id, validated_annotation), (second_id, validated_annotation)),
    )

    with unit_of_work_factory() as unit_of_work:
        update = unit_of_work.annotations.prepare_update(
            first_id,
            _changed(validated_annotation, assigned_by="MGI"),
            expected_version=1,
        )
        create = unit_of_work.annotations.prepare_create(validated_annotation)

    assert update.duplicate_peer_ids == (second_id,)
    assert create.duplicate_peer_ids == (first_id, second_id)


def test_a_prepared_write_from_another_session_is_rejected(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """A write must use a preparation made, with its locks, in the same session."""
    with unit_of_work_factory() as preparing:
        prepared = preparing.annotations.prepare_create(validated_annotation)
        with (
            unit_of_work_factory() as other,
            pytest.raises(ValueError, match="its own session"),
        ):
            other.annotations.create(
                prepared, actor_id="api-user", owning_group_id="group-1"
            )
