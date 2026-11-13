"""Verify GORULE:0000006: IEP and HEP support only biological process terms."""

import pytest
from gorule_cases import TERM, annotation, facts, found, go_rule, term

from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    BP_ROOT,
    CC_ROOT,
)

rule = go_rule("gorule_0000006")

NUCLEUS = "GO:0005634"


@pytest.mark.parametrize("code", ["IEP", "HEP"])
def test_expression_evidence_on_a_non_process_term_is_an_issue(code: str) -> None:
    """IEP or HEP on a cellular component term is reported."""
    subject = annotation(code, ontology_class_id=NUCLEUS, relation="RO:0001025")

    issues = rule(subject, facts({NUCLEUS: term(CC_ROOT)}))

    assert found(issues) == [("gorule_0000006", ("annotation",))]


@pytest.mark.parametrize("term_id", [TERM, BP_ROOT])
def test_expression_evidence_on_a_process_term_passes(term_id: str) -> None:
    """IEP on a term under the BP root, including the root itself, passes."""
    subject = annotation("IEP", ontology_class_id=term_id)

    assert found(rule(subject, facts({term_id: term(BP_ROOT)}))) == []


def test_an_unknown_term_is_not_checked() -> None:
    """Without a current fact for the term, the rule does not fire."""
    subject = annotation("IEP", ontology_class_id=NUCLEUS, relation="RO:0001025")

    assert found(rule(subject, facts({}))) == []
