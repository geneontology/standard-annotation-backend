"""Verify GORULE:0000011: ND evidence goes with root terms, and only them."""

import pytest
from gorule_cases import annotation, facts, found, go_rule, term

from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    BP_ROOT,
    CC_ROOT,
    MF_ROOT,
)

rule = go_rule("gorule_0000011")

ROOTS = [MF_ROOT, BP_ROOT, CC_ROOT]


@pytest.mark.parametrize("root", ROOTS)
def test_a_root_term_with_other_evidence_is_an_issue(root: str) -> None:
    """A root annotated with IDA is reported."""
    subject = annotation("IDA", ontology_class_id=root)

    issues = rule(subject, facts({root: term(root)}))

    assert found(issues) == [("gorule_0000011", ("annotation",))]


def test_nd_on_a_non_root_term_is_an_issue() -> None:
    """ND on a descendant of a root is reported."""
    assert found(rule(annotation("ND"), facts())) == [
        ("gorule_0000011", ("annotation",))
    ]


@pytest.mark.parametrize("root", ROOTS)
def test_nd_on_a_root_term_passes(root: str) -> None:
    """ND on each root passes."""
    subject = annotation("ND", ontology_class_id=root)

    assert found(rule(subject, facts({root: term(root)}))) == []


def test_other_evidence_on_a_non_root_term_passes() -> None:
    """IDA on an ordinary term passes."""
    assert found(rule(annotation("IDA"), facts())) == []
