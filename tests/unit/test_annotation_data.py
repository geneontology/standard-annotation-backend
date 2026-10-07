"""Tests for converting annotations into database values."""

from datetime import date

from standard_annotation_backend.domain.duplicate_policy import (
    canonicalize_duplicate_fields,
    duplicate_base_signature,
)
from standard_annotation_backend.persistence.annotation_data import (
    AnnotationPersistenceData,
    MultivaluedFieldValue,
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationStagingRecord,
)


def test_prepare_annotation_normalizes_data_and_multivalued_fields(
    validated_annotation,
) -> None:
    persistence_data = prepare_annotation_for_persistence(validated_annotation)

    assert isinstance(persistence_data, AnnotationPersistenceData)
    assert persistence_data.annotation_data["annotation_date"] == "2026-09-09"
    assert persistence_data.annotation_data["negation"] is None
    assert persistence_data.db_object_id == "UniProtKB:P12345"
    assert persistence_data.negation is False
    assert persistence_data.relation == "RO:0002331"
    assert persistence_data.ontology_class_id == "GO:0008150"
    assert persistence_data.evidence_type == "ECO:0000314"
    assert persistence_data.annotation_date == date(2026, 9, 9)
    assert persistence_data.assigned_by == "GO_Central"
    assert persistence_data.duplicate_base_signature == duplicate_base_signature(
        validated_annotation
    )
    assert persistence_data.multivalued_field_values == (
        MultivaluedFieldValue("interacting_taxon_id", "NCBITaxon:10090"),
        MultivaluedFieldValue("interacting_taxon_id", "NCBITaxon:9606"),
        MultivaluedFieldValue("references", "PMID:1"),
        MultivaluedFieldValue("references", "PMID:2"),
        MultivaluedFieldValue("with_or_from", "UniProtKB:Q1"),
        MultivaluedFieldValue("with_or_from", "UniProtKB:Q2"),
    )
    assert persistence_data.canonical_references == ("PMID:1", "PMID:2")
    assert all(
        row.field_name in {"references", "with_or_from", "interacting_taxon_id"}
        for row in persistence_data.multivalued_field_values
    )
    assert canonicalize_duplicate_fields(validated_annotation).negation is False


def _sample_persistence_data() -> AnnotationPersistenceData:
    return AnnotationPersistenceData(
        annotation_data={"db_object_id": "UniProtKB:P12345"},
        db_object_id="UniProtKB:P12345",
        negation=True,
        relation="RO:0002331",
        ontology_class_id="GO:0008150",
        evidence_type="ECO:0000314",
        annotation_date=date(2026, 9, 9),
        assigned_by="GO_Central",
        duplicate_base_signature="a" * 64,
        multivalued_field_values=(
            MultivaluedFieldValue("references", "PMID:1"),
            MultivaluedFieldValue("with_or_from", "UniProtKB:Q1"),
        ),
        canonical_references=("PMID:1", "PMID:2"),
    )


def test_column_values_hold_the_stored_data_and_searchable_columns() -> None:
    """Column values cover the stored data and every searchable column."""
    values = _sample_persistence_data().column_values()

    assert values == {
        "annotation_data": {"db_object_id": "UniProtKB:P12345"},
        "duplicate_base_signature": "a" * 64,
        "db_object_id": "UniProtKB:P12345",
        "negation": True,
        "relation": "RO:0002331",
        "ontology_class_id": "GO:0008150",
        "evidence_type": "ECO:0000314",
        "annotation_date": date(2026, 9, 9),
        "assigned_by": "GO_Central",
    }
    for table in (AnnotationRecord.__table__, AnnotationStagingRecord.__table__):
        assert set(values) <= set(table.columns.keys())


def test_multivalued_rows_have_one_row_per_value_in_order() -> None:
    """Each multivalued field value becomes one row, in the stored order."""
    assert _sample_persistence_data().multivalued_rows() == [
        {"field_name": "references", "field_value": "PMID:1"},
        {"field_name": "with_or_from", "field_value": "UniProtKB:Q1"},
    ]


def test_reference_rows_pair_each_reference_with_the_signature() -> None:
    """Each canonical reference becomes one row carrying the duplicate signature."""
    assert _sample_persistence_data().reference_rows() == [
        {"canonical_reference": "PMID:1", "duplicate_base_signature": "a" * 64},
        {"canonical_reference": "PMID:2", "duplicate_base_signature": "a" * 64},
    ]
