"""Tests for storing comments on specific annotation versions."""

from collections.abc import Callable
from datetime import UTC
from uuid import UUID, uuid4

import pytest
from seeding import insert_annotation
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
)
from standard_annotation_backend.persistence.locks import (
    acquire_global_annotation_write_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AnnotationRecord,
    AnnotationVersionRecord,
)
from standard_annotation_backend.persistence.repositories import (
    AnnotationCommentRepository,
    AnnotationDeletedError,
    AnnotationNotFoundError,
    AnnotationRepository,
    CommentNotFoundError,
    InvalidCommentError,
    Page,
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
        record = insert_annotation(
            unit_of_work.annotations.session,
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
        page = unit_of_work.comments.list(annotation_id, limit=50, offset=0)
        listed_comment_ids = [comment.comment_id for comment in page.items]

    assert listed_comment_ids == expected_comment_ids
    assert page.total == 2
    assert _annotation_version_state(session_factory, annotation_id) == before_versions


def test_list_page_applies_offset_and_reports_total(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """A comment page skips requested rows and reports the unpaged total."""
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        comments = tuple(
            unit_of_work.comments.create(
                annotation_id,
                body=f"Comment {index}",
                created_by="reviewer",
            )
            for index in range(3)
        )
        expected_ids = [comment.comment_id for comment in comments[1:]]
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        page = unit_of_work.comments.list(annotation_id, limit=50, offset=1)
        listed_ids = [comment.comment_id for comment in page.items]

    assert listed_ids == expected_ids
    assert page.total == 3


def test_comment_page_total_and_items_share_one_database_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    """A concurrent insert cannot make a comment page contradict its total."""
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        first = unit_of_work.comments.create(
            annotation_id,
            body="First comment",
            created_by="reviewer",
        )
        unit_of_work.commit()
        first_id = first.comment_id

    writer_calls = 0
    with unit_of_work_factory() as reader:

        def insert_after_page_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            *_args: object,
        ) -> None:
            nonlocal writer_calls
            if "count(*) AS total" not in statement:
                return
            writer_calls += 1
            with unit_of_work_factory() as writer:
                writer.comments.create(
                    annotation_id,
                    body="Concurrent comment",
                    created_by="other-reviewer",
                )
                writer.commit()

        event.listen(
            reader.comments.session.connection(),
            "after_cursor_execute",
            insert_after_page_statement,
        )
        page = reader.comments.list(annotation_id, limit=10, offset=0)
        assert isinstance(page, Page)
        page_ids = [record.comment_id for record in page.items]

    assert writer_calls == 1
    assert page.total == 1
    assert page_ids == [first_id]

    with unit_of_work_factory() as unit_of_work:
        stored_count = unit_of_work.comments.session.scalar(
            select(func.count())
            .select_from(AnnotationCommentRecord)
            .where(AnnotationCommentRecord.annotation_id == annotation_id)
        )
    assert stored_count == 2


@pytest.mark.usefixtures("active_annotation_subjects")
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
            expected_version=1,
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
            expected_version=2,
            actor_id="editor",
            change_source="api",
        )
        unit_of_work.commit()

    with session_factory() as session:
        retained = session.get(AnnotationCommentRecord, comment_id)
        assert retained is not None
        assert retained.deleted_at is None
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
        edited = unit_of_work.comments.edit(
            annotation_id,
            comment_id,
            body="Corrected body",
        )
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
        deleted = unit_of_work.comments.soft_delete(annotation_id, comment_id)
        unit_of_work.commit()
        assert deleted.deleted_at is not None
        assert deleted.updated_at == deleted.deleted_at

    with unit_of_work_factory() as unit_of_work:
        assert (
            unit_of_work.comments.get_for_annotation(annotation_id, comment_id) is None
        )
        visible_page = unit_of_work.comments.list(
            annotation_id,
            limit=50,
            offset=0,
        )

    with session_factory() as session:
        retained = session.get(AnnotationCommentRecord, comment_id)
        assert retained is not None
        assert retained.deleted_at is not None
        stored_comment_ids = list(
            session.scalars(
                select(AnnotationCommentRecord.comment_id).where(
                    AnnotationCommentRecord.annotation_id == annotation_id
                )
            )
        )

    assert visible_page.items == ()
    assert visible_page.total == 0
    assert stored_comment_ids == [comment_id]
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
    session_factory: sessionmaker[Session],
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
        unit_of_work.comments.edit(annotation_id, comment_id, body=" \n ")

    with session_factory() as session:
        retained = session.get(AnnotationCommentRecord, comment_id)
        assert retained is not None
        assert retained.deleted_at is None
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
            expected_version=1,
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
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    missing_id = uuid4()
    with unit_of_work_factory() as unit_of_work:
        with pytest.raises(CommentNotFoundError):
            unit_of_work.comments.edit(annotation_id, missing_id, body="Missing")
        with pytest.raises(CommentNotFoundError):
            unit_of_work.comments.soft_delete(annotation_id, missing_id)

    with unit_of_work_factory() as unit_of_work:
        comment = unit_of_work.comments.create(
            annotation_id,
            body="Delete once",
            created_by="reviewer",
        )
        unit_of_work.commit()
        comment_id = comment.comment_id

    with unit_of_work_factory() as unit_of_work:
        unit_of_work.comments.soft_delete(annotation_id, comment_id)
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        with pytest.raises(CommentNotFoundError):
            unit_of_work.comments.edit(
                annotation_id,
                comment_id,
                body="Cannot revive",
            )
        with pytest.raises(CommentNotFoundError):
            unit_of_work.comments.soft_delete(annotation_id, comment_id)


@pytest.mark.parametrize("operation", ["create", "edit", "delete"])
def test_comment_mutations_wait_for_the_global_annotation_write_lock(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    operation: str,
) -> None:
    """Every comment mutation waits while the global exclusive lock is held."""
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    comment_id: UUID | None = None
    if operation != "create":
        with unit_of_work_factory() as unit_of_work:
            comment = unit_of_work.comments.create(
                annotation_id,
                body="Original body",
                created_by="reviewer",
            )
            unit_of_work.commit()
            comment_id = comment.comment_id

    with session_factory() as holder, session_factory() as contender:
        acquire_global_annotation_write_lock(holder, exclusive=True)
        contender.execute(text("SET LOCAL lock_timeout = '100ms'"))
        repository = AnnotationCommentRepository(contender)

        with pytest.raises(DBAPIError, match="lock timeout"):
            if operation == "create":
                repository.create(
                    annotation_id,
                    body="Created after waiting",
                    created_by="reviewer",
                )
            elif operation == "edit":
                assert comment_id is not None
                repository.edit(
                    annotation_id,
                    comment_id,
                    body="Edited after waiting",
                )
            else:
                assert comment_id is not None
                repository.soft_delete(annotation_id, comment_id)
        contender.rollback()

    with unit_of_work_factory() as unit_of_work:
        if operation == "create":
            changed = unit_of_work.comments.create(
                annotation_id,
                body="Created after waiting",
                created_by="reviewer",
            )
        elif operation == "edit":
            assert comment_id is not None
            changed = unit_of_work.comments.edit(
                annotation_id,
                comment_id,
                body="Edited after waiting",
            )
        else:
            assert comment_id is not None
            changed = unit_of_work.comments.soft_delete(annotation_id, comment_id)
        changed_body = changed.body
        changed_deleted_at = changed.deleted_at
        unit_of_work.commit()

    if operation == "delete":
        assert changed_deleted_at is not None
    else:
        assert changed_body.endswith("after waiting")


@pytest.mark.parametrize("operation", ["list", "edit", "delete"])
def test_comment_access_waits_for_an_in_progress_parent_deletion(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    operation: str,
) -> None:
    """Comment access waits until an in-progress parent deletion finishes."""
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        comment = unit_of_work.comments.create(
            annotation_id,
            body="Original body",
            created_by="reviewer",
        )
        unit_of_work.commit()
        comment_id = comment.comment_id

    with session_factory() as holder, session_factory() as contender:
        AnnotationRepository(holder).soft_delete(
            annotation_id,
            expected_version=1,
            actor_id="deleter",
            change_source="api",
        )
        contender.execute(text("SET LOCAL lock_timeout = '100ms'"))
        repository = AnnotationCommentRepository(contender)

        with pytest.raises(DBAPIError, match="lock timeout"):
            if operation == "list":
                repository.list(annotation_id, limit=50, offset=0)
            elif operation == "edit":
                repository.edit(
                    annotation_id,
                    comment_id,
                    body="Edited after waiting",
                )
            else:
                repository.soft_delete(annotation_id, comment_id)
        contender.rollback()

    with unit_of_work_factory() as unit_of_work:
        if operation == "list":
            page = unit_of_work.comments.list(annotation_id, limit=50, offset=0)
            assert [record.comment_id for record in page.items] == [comment_id]
        elif operation == "edit":
            edited = unit_of_work.comments.edit(
                annotation_id,
                comment_id,
                body="Edited after waiting",
            )
            assert edited.body == "Edited after waiting"
        else:
            deleted = unit_of_work.comments.soft_delete(annotation_id, comment_id)
            assert deleted.deleted_at is not None


@pytest.mark.parametrize("operation", ["list", "edit", "delete"])
def test_comment_access_refreshes_a_parent_deleted_after_an_earlier_read(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
    operation: str,
) -> None:
    """A committed parent deletion invalidates a previously loaded active record."""
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)
    with unit_of_work_factory() as unit_of_work:
        comment = unit_of_work.comments.create(
            annotation_id,
            body="Original body",
            created_by="reviewer",
        )
        unit_of_work.commit()
        comment_id = comment.comment_id

    with session_factory() as holder, session_factory() as contender:
        preloaded_parent = contender.get(AnnotationRecord, annotation_id)
        assert preloaded_parent is not None

        AnnotationRepository(holder).soft_delete(
            annotation_id,
            expected_version=1,
            actor_id="deleter",
            change_source="api",
        )
        holder.commit()

        repository = AnnotationCommentRepository(contender)
        with pytest.raises(AnnotationDeletedError):
            if operation == "list":
                repository.list(annotation_id, limit=50, offset=0)
            elif operation == "edit":
                repository.edit(
                    annotation_id,
                    comment_id,
                    body="Disallowed edit",
                )
            else:
                repository.soft_delete(annotation_id, comment_id)
        contender.rollback()

    with session_factory() as session:
        retained = session.get(AnnotationCommentRecord, comment_id)
        assert retained is not None
        assert retained.body == "Original body"
        assert retained.deleted_at is None


def test_unit_of_work_rolls_back_uncommitted_comment_changes(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    annotation_id = _create_annotation(unit_of_work_factory, validated_annotation)

    with unit_of_work_factory() as unit_of_work:
        uncommitted = unit_of_work.comments.create(
            annotation_id,
            body="Never committed",
            created_by="reviewer",
        )
        comment_id = uncommitted.comment_id

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
        unit_of_work.comments.edit(
            annotation_id,
            retained_id,
            body="Rolled-back edit",
        )

    with session_factory() as session:
        retained = session.get(AnnotationCommentRecord, retained_id)
        assert retained is not None
        assert retained.deleted_at is None
        retained_body = retained.body
    assert retained_body == "Committed body"

    with unit_of_work_factory() as unit_of_work:
        unit_of_work.comments.soft_delete(annotation_id, retained_id)

    with session_factory() as session:
        retained = session.get(AnnotationCommentRecord, retained_id)
        assert retained is not None
        retained_deleted_at = retained.deleted_at
    assert retained_deleted_at is None
