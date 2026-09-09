"""Normalize annotations and determine whether they are duplicates."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict

from standard_annotation_backend.domain.annotations import Annotation


class CanonicalDuplicateFields(BaseModel):
    """Normalized annotation values used for duplicate comparison.

    Attributes:
        db_object_id: Identifier of the annotated entity.
        negation: Whether the annotation negates its assertion.
        relation: Relation connecting the entity to the ontology class.
        ontology_class_id: Identifier of the annotated ontology class.
        evidence_type: Identifier of the supporting evidence type.
        references: Sorted, unique identifiers for supporting references.
        with_or_from: Sorted, unique supporting entity identifiers.
        interacting_taxon_id: Sorted, unique interacting taxon identifiers.
        annotation_extensions: Sorted, unique relation and term pairs.
    """

    model_config = ConfigDict(frozen=True)

    db_object_id: str
    negation: bool
    relation: str
    ontology_class_id: str
    evidence_type: str
    references: tuple[str, ...]
    with_or_from: tuple[str, ...]
    interacting_taxon_id: tuple[str, ...]
    annotation_extensions: tuple[tuple[str, str], ...]


def _canonical_string_set(values: list[str] | None) -> tuple[str, ...]:
    """Return unique string values in deterministic order."""
    return tuple(sorted(set(values or ())))


def canonicalize_duplicate_fields(annotation: Annotation) -> CanonicalDuplicateFields:
    """Normalize the annotation fields used for duplicate comparison.

    Missing list values become empty tuples, repeated values are removed, and
    list order is normalized. A missing negation value becomes False.

    Args:
        annotation: Schema-validated annotation to normalize.

    Returns:
        Values used to compare the annotation with another annotation.
    """
    extensions = tuple(
        sorted(
            {
                (extension.extension_relation, extension.extension_term)
                for extension in annotation.annotation_extensions or ()
            }
        )
    )
    return CanonicalDuplicateFields(
        db_object_id=annotation.db_object_id,
        negation=annotation.negation or False,
        relation=annotation.relation,
        ontology_class_id=annotation.ontology_class_id,
        evidence_type=annotation.evidence_type,
        references=_canonical_string_set(annotation.references),
        with_or_from=_canonical_string_set(annotation.with_or_from),
        interacting_taxon_id=_canonical_string_set(annotation.interacting_taxon_id),
        annotation_extensions=extensions,
    )


def duplicate_base_signature(annotation: Annotation) -> str:
    """Create a stable signature for duplicate fields other than references.

    References are excluded because duplicate detection compares them by set
    intersection after matching this signature.

    Args:
        annotation: Schema-validated annotation to sign.

    Returns:
        Lowercase hexadecimal SHA-256 digest of the normalized field values.
    """
    canonical = canonicalize_duplicate_fields(annotation)
    base_fields = canonical.model_dump(mode="json", exclude={"references"})
    encoded = json.dumps(
        base_fields, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def are_duplicate_annotations(left: Annotation, right: Annotation) -> bool:
    """Compare two annotation payloads using the duplicate policy.

    This function cannot inspect SAB identifiers or record status. Callers are
    responsible for comparing distinct, active annotation records.

    Args:
        left: First schema-validated annotation.
        right: Second schema-validated annotation.

    Returns:
        True when the normalized non-reference fields match and the reference
        sets overlap; otherwise, False.
    """
    left_fields = canonicalize_duplicate_fields(left)
    right_fields = canonicalize_duplicate_fields(right)
    if duplicate_base_signature(left) != duplicate_base_signature(right):
        return False
    return not set(left_fields.references).isdisjoint(right_fields.references)
