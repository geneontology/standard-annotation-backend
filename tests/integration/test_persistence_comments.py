"""Tests for storing comments on specific annotation versions."""

from collections.abc import Callable
from datetime import UTC
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AnnotationOrigin,
    AnnotationRecord,
    AnnotationVersionRecord,
)
from standard_annotation_backend.persistence.repositories import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
    CommentNotFoundError,
    InvalidCommentError,
)
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork

UnitOfWorkFactory = Callable[[], SqlAlchemyUnitOfWork]


def _changed_annotation(annotation: Annotation, **changes: object) -> Annotation:
    annotation_data = annotation.model_dump(mode="json")
    annotation_data.update(changes)
    return Annotation.model_validate(annotation_data)


def _create_annotation(
    unit_of_work_factory: UnitOfWorkFactory,
    annotation: Annotation,
) -> UUID:
    with unit_of_work_factory() as unit_of_work:
        record = unit_of_work.annotations.create(
            annotation=annotation,
            actor_id="creator",
            change_source="api",
            owning_group_id="group-1",
            record_origin=AnnotationOrigin.DIRECT,
        )
        unit_of_work.commit()
        return record.annotation_id


def _annotation_version_state(
    session_factory: sessionmaker[Session],
    annotation_id: UUID,
) -> tuple[int, int]:
    with session_factory() as session:
        current_version = session.scalar(
            select(AnnotationRecord.current_version).where(
                AnnotationRecord.annotation_id == annotation_id
            )
        )
        version_count = session.scalar(
            select(func.count())
            .select_from(AnnotationVersionRecord)
            .where(AnnotationVersionRecord.annotation_id == annotation_id)
        )
    assert current_version is not None
    assert version_count is not None
    return current_version, version_count


def test_create_pins_current_version_and_lists_two_comments_deterministically(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    before_versions = _annotation_version_state(session_factory, annotation_id)

    with unit_of_work_factory() as unit_of_work:
        assert unit_of_work.comments.session is unit_of_work.annotations.session
        first = unit_of_work.comments.create(
            annotation_id,
            body="First observation",
            created_by="reviewer-1",
        )
        second = unit_of_work.comments.create(
            annotation_id,
            body="Second observation",
            created_by="reviewer-2",
        )
        unit_of_work.commit()

        assert first.annotation_version == 1
        assert second.annotation_version == 1
        assert first.comment_id != second.comment_id
        assert first.created_at.tzinfo is UTC
        assert first.updated_at == first.created_at
        expected_comment_ids = [first.comment_id, second.comment_id]

    with unit_of_work_factory() as unit_of_work:
        listed = unit_of_work.comments.list(annotation_id)
        listed_comment_ids = [comment.comment_id for comment in listed]

    assert listed_comment_ids == expected_comment_ids
    assert _annotation_version_state(session_factory, annotation_id) == before_versions


def test_create_pins_actual_current_version_and_remains_pinned(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    version_two = _changed_annotation(validated_annotation, assigned_by="Version_Two")
    with unit_of_work_factory() as unit_of_work:
        unit_of_work.annotations.update(
            annotation_id,
            version_two,
            actor_id="editor",
            change_source="api",
        )
        unit_of_work.commit()

    assert _annotation_version_state(session_factory, annotation_id) == (2, 2)
    with unit_of_work_factory() as unit_of_work:
        comment = unit_of_work.comments.create(
            annotation_id,
            body="Observed version two",
            created_by="reviewer",
        )
        unit_of_work.commit()
        comment_id = comment.comment_id
        assert comment.annotation_version == 2

    assert _annotation_version_state(session_factory, annotation_id) == (2, 2)
    version_three = _changed_annotation(
        validated_annotation, assigned_by="Version_Three"
    )
    with unit_of_work_factory() as unit_of_work:
        unit_of_work.annotations.update(
            annotation_id,
            version_three,
            actor_id="editor",
            change_source="api",
        )
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        retained = unit_of_work.comments.get(comment_id)
        assert retained is not None
        retained_version = retained.annotation_version

    assert retained_version == 2
    assert _annotation_version_state(session_factory, annotation_id) == (3, 3)


def test_edit_changes_body_and_timestamp_but_preserves_creator_and_version(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        comment = unit_of_work.comments.create(
            annotation_id,
            body="Original body",
            created_by="original-author",
        )
        unit_of_work.commit()
        comment_id = comment.comment_id
        original_updated_at = comment.updated_at
        original_created_at = comment.created_at

    before_versions = _annotation_version_state(session_factory, annotation_id)
    with unit_of_work_factory() as unit_of_work:
        edited = unit_of_work.comments.edit(comment_id, body="Corrected body")
        unit_of_work.commit()
        assert edited.body == "Corrected body"
        assert edited.updated_at > original_updated_at
        assert edited.created_at == original_created_at
        assert edited.created_by == "original-author"
        assert edited.annotation_id == annotation_id
        assert edited.annotation_version == 1

    assert _annotation_version_state(session_factory, annotation_id) == before_versions


def test_soft_delete_hides_comment_without_changing_annotation_versions(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        comment = unit_of_work.comments.create(
            annotation_id,
            body="Temporary note",
            created_by="reviewer",
        )
        unit_of_work.commit()
        comment_id = comment.comment_id

    before_versions = _annotation_version_state(session_factory, annotation_id)
    with unit_of_work_factory() as unit_of_work:
        deleted = unit_of_work.comments.soft_delete(comment_id)
        unit_of_work.commit()
        assert deleted.deleted_at is not None
        assert deleted.updated_at == deleted.deleted_at

    with unit_of_work_factory() as unit_of_work:
        assert unit_of_work.comments.get(comment_id) is None
        assert unit_of_work.comments.list(annotation_id) == ()
        retained = unit_of_work.comments.get(comment_id, include_deleted=True)
        listed = unit_of_work.comments.list(annotation_id, include_deleted=True)
        assert retained is not None
        retained_comment_id = retained.comment_id
        listed_comment_ids = [comment.comment_id for comment in listed]

    assert retained_comment_id == comment_id
    assert listed_comment_ids == [comment_id]
    assert _annotation_version_state(session_factory, annotation_id) == before_versions


@pytest.mark.parametrize("body", ["", "   ", "\t\n"])
def test_blank_create_body_raises_before_database_work(
    unit_of_work_factory: UnitOfWorkFactory,
    body: str,
) -> None:
    with unit_of_work_factory() as unit_of_work, pytest.raises(InvalidCommentError):
        unit_of_work.comments.create(
            uuid4(),
            body=body,
            created_by="reviewer",
        )


def test_blank_edit_body_is_rejected_without_changing_comment(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        comment = unit_of_work.comments.create(
            annotation_id,
            body="Retained body",
            created_by="reviewer",
        )
        unit_of_work.commit()
        comment_id = comment.comment_id

    with unit_of_work_factory() as unit_of_work, pytest.raises(InvalidCommentError):
        unit_of_work.comments.edit(comment_id, body=" \n ")

    with unit_of_work_factory() as unit_of_work:
        retained = unit_of_work.comments.get(comment_id)
        assert retained is not None
        retained_body = retained.body
    assert retained_body == "Retained body"


def test_create_rejects_missing_and_deleted_annotations(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    with unit_of_work_factory() as unit_of_work, pytest.raises(AnnotationNotFoundError):
        unit_of_work.comments.create(
            uuid4(),
            body="No target",
            created_by="reviewer",
        )

    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        unit_of_work.annotations.soft_delete(
            annotation_id,
            actor_id="deleter",
            change_source="api",
        )
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work, pytest.raises(AnnotationDeletedError):
        unit_of_work.comments.create(
            annotation_id,
            body="Deleted target",
            created_by="reviewer",
        )


def test_missing_and_already_deleted_comment_transitions_raise_not_found(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    missing_id = uuid4()
    with unit_of_work_factory() as unit_of_work:
        with pytest.raises(CommentNotFoundError):
            unit_of_work.comments.edit(missing_id, body="Missing")
        with pytest.raises(CommentNotFoundError):
            unit_of_work.comments.soft_delete(missing_id)

    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        comment = unit_of_work.comments.create(
            annotation_id,
            body="Delete once",
            created_by="reviewer",
        )
        unit_of_work.commit()
        comment_id = comment.comment_id

    with unit_of_work_factory() as unit_of_work:
        unit_of_work.comments.soft_delete(comment_id)
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        with pytest.raises(CommentNotFoundError):
            unit_of_work.comments.edit(comment_id, body="Cannot revive")
        with pytest.raises(CommentNotFoundError):
            unit_of_work.comments.soft_delete(comment_id)


def test_unit_of_work_rolls_back_uncommitted_comment_changes(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    comment_id = uuid4()

    with unit_of_work_factory() as unit_of_work:
        unit_of_work.comments.create(
            annotation_id,
            body="Never committed",
            created_by="reviewer",
            comment_id=comment_id,
        )

    with session_factory() as session:
        assert session.get(AnnotationCommentRecord, comment_id) is None

    with unit_of_work_factory() as unit_of_work:
        comment = unit_of_work.comments.create(
            annotation_id,
            body="Committed body",
            created_by="reviewer",
        )
        unit_of_work.commit()
        retained_id = comment.comment_id

    with unit_of_work_factory() as unit_of_work:
        unit_of_work.comments.edit(retained_id, body="Rolled-back edit")

    with unit_of_work_factory() as unit_of_work:
        retained = unit_of_work.comments.get(retained_id)
        assert retained is not None
        retained_body = retained.body
    assert retained_body == "Committed body"

    with unit_of_work_factory() as unit_of_work:
        unit_of_work.comments.soft_delete(retained_id)

    with unit_of_work_factory() as unit_of_work:
        retained = unit_of_work.comments.get(retained_id)
        assert retained is not None
        retained_deleted_at = retained.deleted_at
    assert retained_deleted_at is None
