"""Test database queries for active annotations and their saved versions."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence import repositories
from standard_annotation_backend.persistence.models import AnnotationOrigin
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
    record = unit_of_work.annotations.create(
        annotation=annotation,
        actor_id="creator",
        change_source="test",
        owning_group_id="group-1",
        record_origin=AnnotationOrigin.DIRECT,
        annotation_id=annotation_id,
    )
    record.created_at = created_at


def _scalar_filters(
    field_name: str,
    field_value: object,
) -> repositories.AnnotationSearchFilters:
    if field_name == "negation":
        assert isinstance(field_value, bool)
        return repositories.AnnotationSearchFilters(negation=field_value)
    if field_name == "annotation_date":
        assert isinstance(field_value, date)
        return repositories.AnnotationSearchFilters(annotation_date=field_value)

    assert isinstance(field_value, str)
    if field_name == "db_object_id":
        return repositories.AnnotationSearchFilters(db_object_id=field_value)
    if field_name == "relation":
        return repositories.AnnotationSearchFilters(relation=field_value)
    if field_name == "ontology_class_id":
        return repositories.AnnotationSearchFilters(ontology_class_id=field_value)
    if field_name == "evidence_type":
        return repositories.AnnotationSearchFilters(evidence_type=field_value)
    if field_name == "assigned_by":
        return repositories.AnnotationSearchFilters(assigned_by=field_value)
    raise AssertionError(f"unsupported scalar filter {field_name}")


def _multivalued_filters(
    field_name: str,
    values: tuple[str, ...],
) -> repositories.AnnotationSearchFilters:
    if field_name == "references":
        return repositories.AnnotationSearchFilters(references=values)
    if field_name == "with_or_from":
        return repositories.AnnotationSearchFilters(with_or_from=values)
    if field_name == "interacting_taxon_id":
        return repositories.AnnotationSearchFilters(interacting_taxon_id=values)
    raise AssertionError(f"unsupported multivalued filter {field_name}")


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
            repositories.AnnotationSearchFilters(assigned_by="MGI"),
            limit=2,
            offset=1,
        )
        assert page.total == 3
        assert [record.annotation_id for record in page.items] == [
            earlier_mgi_id,
            later_mgi_id,
        ]


def test_scalar_search_excludes_soft_deleted_rows_from_items_and_total(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    deleted_id = UUID("00000000-0000-0000-0000-000000000021")
    active_id = UUID("00000000-0000-0000-0000-000000000022")
    with unit_of_work_factory() as unit_of_work:
        for position, annotation_id in enumerate((deleted_id, active_id), start=1):
            _create(
                unit_of_work,
                _changed(
                    validated_annotation,
                    db_object_id=f"MGI:{position}",
                    assigned_by="MGI",
                ),
                annotation_id=annotation_id,
                created_at=datetime(2026, 2, position, tzinfo=UTC),
            )
        unit_of_work.commit()
    with unit_of_work_factory() as unit_of_work:
        unit_of_work.annotations.soft_delete(
            deleted_id,
            actor_id="deleter",
            change_source="test",
        )
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        page = unit_of_work.annotations.list_active(
            repositories.AnnotationSearchFilters(assigned_by="MGI"),
            limit=10,
            offset=0,
        )
        assert page.total == 1
        assert [record.annotation_id for record in page.items] == [active_id]


@pytest.mark.parametrize(
    ("filter_name", "filter_value"),
    [
        ("db_object_id", "UniProtKB:P12345"),
        ("negation", False),
        ("relation", "RO:0002331"),
        ("ontology_class_id", "GO:0008150"),
        ("evidence_type", "ECO:0000314"),
        ("annotation_date", date(2026, 9, 9)),
        ("assigned_by", "GO_Central"),
    ],
)
def test_scalar_search_applies_each_supported_filter(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    filter_name: str,
    filter_value: object,
) -> None:
    matching_id = UUID("00000000-0000-0000-0000-000000000031")
    with unit_of_work_factory() as unit_of_work:
        _create(
            unit_of_work,
            validated_annotation,
            annotation_id=matching_id,
            created_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
        _create(
            unit_of_work,
            _changed(
                validated_annotation,
                db_object_id="MGI:other",
                negation=True,
                relation="RO:0002327",
                ontology_class_id="GO:0000001",
                evidence_type="ECO:0000250",
                annotation_date="2026-09-10",
                assigned_by="MGI",
            ),
            annotation_id=UUID("00000000-0000-0000-0000-000000000032"),
            created_at=datetime(2026, 3, 2, tzinfo=UTC),
        )
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        page = unit_of_work.annotations.list_active(
            _scalar_filters(filter_name, filter_value),
            limit=10,
            offset=0,
        )
        assert page.total == 1
        assert [record.annotation_id for record in page.items] == [matching_id]


@pytest.mark.parametrize(
    ("field_name", "first_value", "second_value"),
    [
        ("references", "PMID:1", "PMID:2"),
        ("with_or_from", "UniProtKB:Q1", "UniProtKB:Q2"),
        (
            "interacting_taxon_id",
            "NCBITaxon:9606",
            "NCBITaxon:10090",
        ),
    ],
)
def test_multivalued_search_requires_every_requested_value(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
    field_name: str,
    first_value: str,
    second_value: str,
) -> None:
    both_values_id = UUID("00000000-0000-0000-0000-000000000043")
    with unit_of_work_factory() as unit_of_work:
        for position, values in enumerate(
            ([first_value], [second_value], [first_value, second_value]),
            start=1,
        ):
            _create(
                unit_of_work,
                _changed(validated_annotation, **{field_name: values}),
                annotation_id=UUID(f"00000000-0000-0000-0000-{40 + position:012d}"),
                created_at=datetime(2026, 4, position, tzinfo=UTC),
            )
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        page = unit_of_work.annotations.list_active(
            _multivalued_filters(field_name, (first_value, second_value)),
            limit=10,
            offset=0,
        )
        assert page.total == 1
        assert [record.annotation_id for record in page.items] == [both_values_id]


def test_multivalued_search_combines_with_scalar_filters(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    matching_id = UUID("00000000-0000-0000-0000-000000000051")
    with unit_of_work_factory() as unit_of_work:
        candidates = (
            (matching_id, "MGI", ["PMID:1", "PMID:2"]),
            (
                UUID("00000000-0000-0000-0000-000000000052"),
                "RGD",
                ["PMID:1", "PMID:2"],
            ),
            (
                UUID("00000000-0000-0000-0000-000000000053"),
                "MGI",
                ["PMID:1"],
            ),
        )
        for position, (annotation_id, assigned_by, references) in enumerate(
            candidates,
            start=1,
        ):
            _create(
                unit_of_work,
                _changed(
                    validated_annotation,
                    db_object_id=f"MGI:{position}",
                    assigned_by=assigned_by,
                    references=references,
                ),
                annotation_id=annotation_id,
                created_at=datetime(2026, 5, position, tzinfo=UTC),
            )
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        page = unit_of_work.annotations.list_active(
            repositories.AnnotationSearchFilters(
                assigned_by="MGI",
                references=("PMID:1", "PMID:2"),
            ),
            limit=10,
            offset=0,
        )
        assert page.total == 1
        assert [record.annotation_id for record in page.items] == [matching_id]


def test_version_queries_include_deleted_annotation_and_paginate_oldest_first(
    unit_of_work_factory: UnitOfWorkFactory,
    validated_annotation: Annotation,
) -> None:
    annotation_id = UUID("00000000-0000-0000-0000-000000000061")
    with unit_of_work_factory() as unit_of_work:
        _create(
            unit_of_work,
            validated_annotation,
            annotation_id=annotation_id,
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        unit_of_work.commit()
    with unit_of_work_factory() as unit_of_work:
        unit_of_work.annotations.update(
            annotation_id,
            _changed(validated_annotation, assigned_by="MGI"),
            actor_id="editor",
            change_source="test",
        )
        unit_of_work.commit()
    with unit_of_work_factory() as unit_of_work:
        unit_of_work.annotations.soft_delete(
            annotation_id,
            actor_id="deleter",
            change_source="test",
        )
        unit_of_work.commit()

    with unit_of_work_factory() as unit_of_work:
        assert (
            unit_of_work.annotations.annotation_exists(
                annotation_id,
                include_deleted=True,
            )
            is True
        )
        assert (
            unit_of_work.annotations.annotation_exists(
                annotation_id,
                include_deleted=False,
            )
            is False
        )
        assert (
            unit_of_work.annotations.annotation_exists(
                uuid4(),
                include_deleted=True,
            )
            is False
        )

        page = unit_of_work.annotations.list_versions_page(
            annotation_id,
            limit=2,
            offset=1,
        )
        assert page.total == 3
        assert [(item.version, item.is_deleted) for item in page.items] == [
            (2, False),
            (3, True),
        ]

        deleted_version = unit_of_work.annotations.get_version(annotation_id, 3)
        assert deleted_version is not None
        assert deleted_version.is_deleted is True
        assert unit_of_work.annotations.get_version(annotation_id, 4) is None
