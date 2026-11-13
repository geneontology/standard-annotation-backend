"""Verify GORULE:0000046: self-binding annotations name their own subject."""

import pytest
from gorule_cases import annotation, facts, found, go_rule, term

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    MF_ROOT,
    SELF_BINDING_TERMS,
)

rule = go_rule("gorule_0000046")


def _self_binding(term_id: str, with_or_from: list[str]) -> Annotation:
    return annotation(
        "IPI",
        ontology_class_id=term_id,
        relation="RO:0002327",
        with_or_from=with_or_from,
    )


@pytest.mark.parametrize("self_binding", sorted(SELF_BINDING_TERMS))
def test_values_without_the_subject_are_an_issue(self_binding: str) -> None:
    """A term under any self-binding term must list `db_object_id` if it lists anything."""
    subject = _self_binding(self_binding, ["UniProtKB:Q1"])

    issues = rule(subject, facts({self_binding: term(MF_ROOT, self_binding)}))

    assert found(issues) == [("gorule_0000046", ("annotation",))]


@pytest.mark.parametrize("with_or_from", [["UniProtKB:Q1", "UniProtKB:P12345"], []])
def test_values_with_the_subject_or_no_values_pass(with_or_from: list[str]) -> None:
    """Listing the subject, or nothing at all, passes."""
    self_binding = "GO:0042803"
    subject = _self_binding(self_binding, with_or_from)

    assert (
        found(rule(subject, facts({self_binding: term(MF_ROOT, self_binding)}))) == []
    )


def test_other_terms_pass() -> None:
    """A term under no self-binding term is not checked."""
    subject = _self_binding("GO:0005515", ["UniProtKB:Q1"])

    assert found(rule(subject, facts({"GO:0005515": term(MF_ROOT)}))) == []
