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
