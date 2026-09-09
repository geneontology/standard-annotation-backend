"""Tests for Standard Annotation duplicate detection and normalization."""

from collections.abc import Mapping

import pytest

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.duplicate_policy import (
    are_duplicate_annotations,
    canonicalize_duplicate_fields,
    duplicate_base_signature,
)


def annotation(**overrides: object) -> Annotation:
    """Build a valid annotation with field overrides for a test case."""
    payload: dict[str, object] = {
        "db_object_id": "UniProtKB:P12345",
        "negation": False,
        "relation": "RO:0002331",
        "ontology_class_id": "GO:0008150",
        "references": ["PMID:1"],
        "evidence_type": "ECO:0000314",
        "with_or_from": ["UniProtKB:Q1"],
        "interacting_taxon_id": ["NCBITaxon:9606"],
        "annotation_date": "2026-09-09",
        "assigned_by": "GO_Central",
        "annotation_extensions": [
            {
                "extension_relation": "BFO:0000050",
                "extension_term": "CL:0000000",
            }
        ],
    }
    payload.update(overrides)
    return Annotation.model_validate(payload)


@pytest.mark.parametrize(
    "changed_field",
    [
        {"db_object_id": "UniProtKB:DIFFERENT"},
        {"negation": True},
        {"relation": "RO:0002327"},
        {"ontology_class_id": "GO:0003674"},
        {"evidence_type": "ECO:0000501"},
        {"references": ["PMID:2"]},
        {"with_or_from": ["UniProtKB:Q2"]},
        {"with_or_from": ["UniProtKB:Q1", "UniProtKB:Q2"]},
        {"interacting_taxon_id": ["NCBITaxon:10090"]},
        {
            "interacting_taxon_id": [
                "NCBITaxon:9606",
                "NCBITaxon:10090",
            ]
        },
        {
            "annotation_extensions": [
                {
                    "extension_relation": "BFO:0000050",
                    "extension_term": "CL:0000001",
                }
            ]
        },
        {
            "annotation_extensions": [
                {
                    "extension_relation": "BFO:0000050",
                    "extension_term": "CL:0000000",
                },
                {
                    "extension_relation": "RO:0002233",
                    "extension_term": "CHEBI:1",
                },
            ]
        },
    ],
)
def test_each_participating_field_can_prevent_a_duplicate(
    changed_field: Mapping[str, object],
) -> None:
    assert not are_duplicate_annotations(annotation(), annotation(**changed_field))


def test_excluded_fields_do_not_affect_duplicate_identity() -> None:
    changed = annotation(
        annotation_date="2025-01-01",
        assigned_by="MGI",
        annotation_properties={"comment": ["changed"]},
    )

    assert are_duplicate_annotations(annotation(), changed)


def test_lists_are_sets_for_canonical_duplicate_fields() -> None:
    repeated = annotation(
        references=["PMID:2", "PMID:1", "PMID:1"],
        with_or_from=["UniProtKB:Q2", "UniProtKB:Q1", "UniProtKB:Q1"],
        interacting_taxon_id=[
            "NCBITaxon:10090",
            "NCBITaxon:9606",
            "NCBITaxon:9606",
        ],
        annotation_extensions=[
            {
                "extension_relation": "RO:0002233",
                "extension_term": "CHEBI:1",
            },
            {
                "extension_relation": "BFO:0000050",
                "extension_term": "CL:0000000",
            },
            {
                "extension_relation": "BFO:0000050",
                "extension_term": "CL:0000000",
            },
        ],
    )
    reordered = annotation(
        references=["PMID:1", "PMID:2"],
        with_or_from=["UniProtKB:Q1", "UniProtKB:Q2"],
        interacting_taxon_id=["NCBITaxon:9606", "NCBITaxon:10090"],
        annotation_extensions=[
            {
                "extension_relation": "BFO:0000050",
                "extension_term": "CL:0000000",
            },
            {
                "extension_relation": "RO:0002233",
                "extension_term": "CHEBI:1",
            },
        ],
    )

    assert canonicalize_duplicate_fields(repeated) == canonicalize_duplicate_fields(
        reordered
    )
    assert are_duplicate_annotations(repeated, reordered)


def test_missing_optional_values_match_false_and_empty_lists() -> None:
    missing = annotation(
        negation=None,
        with_or_from=None,
        interacting_taxon_id=None,
        annotation_extensions=None,
    )
    explicit = annotation(
        negation=False,
        with_or_from=[],
        interacting_taxon_id=[],
        annotation_extensions=[],
    )

    assert canonicalize_duplicate_fields(missing) == canonicalize_duplicate_fields(
        explicit
    )
    assert are_duplicate_annotations(missing, explicit)


def test_base_signature_is_stable_and_excludes_references() -> None:
    first = annotation(
        references=["PMID:1"],
        with_or_from=["UniProtKB:Q2", "UniProtKB:Q1"],
    )
    second = annotation(
        references=["GO_REF:0000001"],
        with_or_from=["UniProtKB:Q1", "UniProtKB:Q2"],
    )

    first_signature = duplicate_base_signature(first)

    assert first_signature == duplicate_base_signature(second)
    assert first_signature == (
        "52c5485a7129822f2611f33be80c58a2baf580231799dc42d21226c3743b82d3"
    )


def test_reference_overlap_is_pairwise_and_not_transitive() -> None:
    first = annotation(references=["PMID:1"])
    bridge = annotation(references=["PMID:1", "PMID:2"])
    third = annotation(references=["PMID:2"])

    assert are_duplicate_annotations(first, bridge)
    assert are_duplicate_annotations(bridge, third)
    assert not are_duplicate_annotations(first, third)
