"""Prepare validated annotations and derived values for database storage."""

from dataclasses import dataclass
from datetime import date

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.duplicate_policy import (
    canonicalize_duplicate_fields,
    duplicate_base_signature,
)


@dataclass(frozen=True, slots=True)
class MultivaluedFieldValue:
    """One value from a multivalued annotation field.

    Attributes:
        field_name: Name of the multivalued annotation field.
        field_value: One normalized value from that field.
    """

    field_name: str
    field_value: str


@dataclass(frozen=True, slots=True)
class AnnotationPersistenceData:
    """Data used to store an annotation and support database queries.

    Attributes:
        annotation_data: Complete annotation represented as JSON-compatible values.
        db_object_id: Identifier for the annotated object.
        negation: Whether the annotation is negated.
        relation: Relationship between the object and ontology term.
        ontology_class_id: Identifier for the ontology term.
        evidence_type: Identifier for the evidence type.
        annotation_date: Date assigned to the annotation.
        assigned_by: Group that supplied the annotation.
        duplicate_base_signature: Hash used to find possible duplicates.
        multivalued_field_values: Values from multivalued fields stored for
            database filtering.
        canonical_references: Normalized references used to find duplicates.
    """

    annotation_data: dict[str, object]
    db_object_id: str
    negation: bool
    relation: str
    ontology_class_id: str
    evidence_type: str
    annotation_date: date
    assigned_by: str
    duplicate_base_signature: str
    multivalued_field_values: tuple[MultivaluedFieldValue, ...]
    canonical_references: tuple[str, ...]


def prepare_annotation_for_persistence(
    annotation: Annotation,
) -> AnnotationPersistenceData:
    """Prepare a validated annotation for database storage.

    Args:
        annotation: Validated standard annotation to prepare.

    Returns:
        The annotation data, searchable multivalued field values, duplicate
        signature, and canonical references used by the repositories.

    Raises:
        TypeError: If `annotation` is not an `Annotation` instance.
    """
    if not isinstance(annotation, Annotation):
        raise TypeError(
            "prepare_annotation_for_persistence expects a validated Annotation"
        )

    canonical = canonicalize_duplicate_fields(annotation)
    multivalued_field_values = tuple(
        MultivaluedFieldValue(field_name, field_value)
        for field_name, values in {
            "references": canonical.references,
            "with_or_from": canonical.with_or_from,
            "interacting_taxon_id": canonical.interacting_taxon_id,
        }.items()
        for field_value in values
    )
    multivalued_field_values = tuple(
        sorted(
            multivalued_field_values,
            key=lambda value: (value.field_name, value.field_value),
        )
    )

    return AnnotationPersistenceData(
        annotation_data=annotation.model_dump(mode="json"),
        db_object_id=canonical.db_object_id,
        negation=canonical.negation,
        relation=canonical.relation,
        ontology_class_id=canonical.ontology_class_id,
        evidence_type=canonical.evidence_type,
        annotation_date=annotation.annotation_date,
        assigned_by=annotation.assigned_by,
        duplicate_base_signature=duplicate_base_signature(annotation),
        multivalued_field_values=multivalued_field_values,
        canonical_references=canonical.references,
    )
