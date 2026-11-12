"""Verify GORULE:0000002: annotations to binding terms cannot be negated."""

import pytest
from gorule_cases import annotation, facts, found, term

from standard_annotation_backend.domain.semantic_validation.gorules.terms import (
    gorule_0000002,
)


@pytest.mark.parametrize("term_id", ["GO:0005488", "GO:0005515"])
def test_a_negated_annotation_to_a_binding_term_is_an_issue(term_id: str) -> None:
    """Negating an annotation directly to either binding term is reported."""
    subject = annotation(ontology_class_id=term_id, negation=True)

    issues = gorule_0000002(subject, facts({term_id: term()}))

    assert found(issues) == [("gorule_0000002", ("annotation",))]


@pytest.mark.parametrize(
    ("term_id", "negation"),
    [("GO:0005515", False), ("GO:0005515", None), ("GO:0042803", True)],
)
def test_other_annotations_pass(term_id: str, negation: bool | None) -> None:
    """A binding term that is not negated, or a negated other term, passes."""
    subject = annotation(ontology_class_id=term_id, negation=negation)

    assert found(gorule_0000002(subject, facts({term_id: term()}))) == []
