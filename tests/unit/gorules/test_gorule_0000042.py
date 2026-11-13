"""Verify GORULE:0000042: IKR evidence requires a negated annotation."""

import pytest
from gorule_cases import annotation, facts, found, go_rule

rule = go_rule("gorule_0000042")


@pytest.mark.parametrize("negation", [None, False])
def test_ikr_without_negation_is_an_issue(negation: bool | None) -> None:
    """IKR on an annotation that is not negated is reported."""
    subject = annotation("IKR", negation=negation)

    assert found(rule(subject, facts())) == [("gorule_0000042", ("annotation",))]


def test_negated_ikr_and_other_evidence_pass() -> None:
    """A negated IKR annotation, and IDA without negation, pass."""
    assert found(rule(annotation("IKR", negation=True), facts())) == []
    assert found(rule(annotation("IDA"), facts())) == []
