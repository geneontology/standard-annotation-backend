"""Verify GORULE:0000039: ComplexPortal entities are not annotated to complexes."""

from gorule_cases import annotation, facts, found, go_rule, term

from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    CC_ROOT,
    PROTEIN_CONTAINING_COMPLEX,
)

rule = go_rule("gorule_0000039")

COMPLEX_TERM = "GO:0000786"
COMPLEX_FACTS = {COMPLEX_TERM: term(CC_ROOT, PROTEIN_CONTAINING_COMPLEX)}


def test_a_complex_portal_entity_on_a_complex_term_is_an_issue() -> None:
    """A ComplexPortal subject on a term under `GO:0032991` is reported."""
    subject = annotation(
        db_object_id="ComplexPortal:CPX-1", ontology_class_id=COMPLEX_TERM
    )

    assert found(rule(subject, facts(COMPLEX_FACTS))) == [
        ("gorule_0000039", ("annotation",))
    ]


def test_other_subjects_or_terms_pass() -> None:
    """Another subject on a complex term, or ComplexPortal elsewhere, passes."""
    uniprot = annotation(ontology_class_id=COMPLEX_TERM)
    process = annotation(db_object_id="ComplexPortal:CPX-1")

    assert found(rule(uniprot, facts(COMPLEX_FACTS))) == []
    assert found(rule(process, facts())) == []
