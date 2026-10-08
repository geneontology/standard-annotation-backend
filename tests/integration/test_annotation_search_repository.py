"""Test database queries for active annotations and their saved versions."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest
from seeding import insert_annotation
from sqlalchemy import event

from standard_annotation_backend.domain.annotation_search import (
    AnnotationFilter,
    OwnershipScope,
)
from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    ChangeSource,
)
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork

UnitOfWorkFactory = Callable[[], SqlAlchemyUnitOfWork]


def _changed(annotation: Annotation, **changes: object) -> Annotation:
    annotation_data = annotation.model_dump(mode="json")
    annotation_data.update(changes)
    return Annotation.model_validate(annotation_data)


def _create(
    unit_of_work: SqlAlchemyUnitOfWork,
    annotation: Annotation,
    *,
    annotation_id: UUID,
    created_at: datetime,
) -> None:
    record = insert_annotation(
        unit_of_work.annotations.session,
        annotation=annotation,
        actor_id="creator",
        change_source=ChangeSource.API,
        owning_group_id="group-1",
        record_origin=AnnotationOrigin.DIRECT,
        annotation_id=annotation_id,
    )
    record.created_at = created_at


def test_scalar_search_returns_stable_page_and_exact_total(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    first_mgi_id = UUID("00000000-0000-0000-0000-000000000011")
    later_mgi_id = UUID("00000000-0000-0000-0000-000000000013")
    earlier_mgi_id = UUID("00000000-0000-0000-0000-000000000012")
    shared_time = datetime(2026, 1, 2, tzinfo=UTC)
    with unit_of_work_factory() as unit_of_work:
        _create(
            unit_of_work,
            _changed(
                validated_annotation,
                db_object_id="MGI:1",
                relation="RO:0002331",
                ontology_class_id="GO:0000001",
                evidence_type="ECO:0000314",
                annotation_date="2026-01-01",
                assigned_by="MGI",
            ),
            annotation_id=first_mgi_id,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        _create(
            unit_of_work,
            _changed(
                validated_annotation,
                db_object_id="MGI:2",
                relation="RO:0002331",
                ontology_class_id="GO:0000002",
                evidence_type="ECO:0000314",
                annotation_date="2026-01-02",
                assigned_by="MGI",
            ),
            annotation_id=later_mgi_id,
            created_at=shared_time,
        )
        _create(
            unit_of_work,
            _changed(
                validated_annotation,
                db_object_id="MGI:3",
                relation="RO:0002331",
                ontology_class_id="GO:0000003",
                evidence_type="ECO:0000314",
                annotation_date="2026-01-03",
                assigned_by="MGI",
            ),
            annotation_id=earlier_mgi_id,
            created_at=shared_time,
        )
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        page = unit_of_work.annotations.list_active(
            AnnotationFilter(assigned_by="MGI"),
            OwnershipScope(),
            limit=2,
            offset=1,
        )
        assert page.total == 3
        assert [record.annotation_id for record in page.items] == [
            earlier_mgi_id,
            later_mgi_id,
        ]

        empty_page = unit_of_work.annotations.list_active(
            AnnotationFilter(assigned_by="MGI"),
            OwnershipScope(),
            limit=2,
            offset=3,
        )
        assert empty_page.total == 3
        assert empty_page.items == ()


def test_active_page_total_and_items_share_one_database_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    first_id = UUID("00000000-0000-0000-0000-000000000014")
    concurrent_id = UUID("00000000-0000-0000-0000-000000000015")
    with unit_of_work_factory() as unit_of_work:
        _create(
            unit_of_work,
            _changed(validated_annotation, assigned_by="MGI"),
            annotation_id=first_id,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        unit_of_work.commit()

    writer_calls = 0
    with unit_of_work_factory() as reader:

        def insert_after_reader_statement(*_args: object) -> None:
            nonlocal writer_calls
            writer_calls += 1
            with unit_of_work_factory() as writer:
                _create(
                    writer,
                    _changed(
                        validated_annotation,
                        db_object_id="MGI:concurrent",
                        assigned_by="MGI",
                    ),
                    annotation_id=concurrent_id,
                    created_at=datetime(2026, 1, 2, tzinfo=UTC),
                )
                writer.commit()

        # The event listener is registered to trigger after the first SQL statement
        # executed by the reader's session. This simulates a concurrent write operation
        # that occurs while the reader is fetching data, allowing us to test that the
        # reader's view of the database remains consistent and unaffected by the
        # concurrent write.
        event.listen(
            reader.annotations.session.connection(),
            "after_cursor_execute",
            insert_after_reader_statement,
            once=True,
        )
        page = reader.annotations.list_active(
            AnnotationFilter(assigned_by="MGI"),
            OwnershipScope(),
            limit=10,
            offset=0,
        )
        page_ids = [record.annotation_id for record in page.items]

    assert writer_calls == 1
    assert page.total == 1
    assert page_ids == [first_id]


@pytest.mark.usefixtures("active_annotation_subjects")
def test_version_page_total_and_items_share_one_database_snapshot(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    annotation_id = UUID("00000000-0000-0000-0000-000000000062")
    with unit_of_work_factory() as unit_of_work:
        _create(
            unit_of_work,
            validated_annotation,
            annotation_id=annotation_id,
            created_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        unit_of_work.commit()

    writer_calls = 0
    with unit_of_work_factory() as reader:

        def append_version_after_reader_statement(*_args: object) -> None:
            nonlocal writer_calls
            writer_calls += 1
            with unit_of_work_factory() as writer:
                writer.annotations.update(
                    annotation_id,
                    _changed(validated_annotation, assigned_by="MGI"),
                    expected_version=1,
                    actor_id="concurrent-editor",
                    change_source=ChangeSource.API,
                )
                writer.commit()

        event.listen(
            reader.annotations.session.connection(),
            "after_cursor_execute",
            append_version_after_reader_statement,
            once=True,
        )
        page = reader.annotations.list_versions_page(
            annotation_id,
            limit=10,
            offset=0,
        )
        page_versions = [version.version for version in page.items]

    assert writer_calls == 1
    assert page.total == 1
    assert page_versions == [1]
