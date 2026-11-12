"""Verify GORULE:0000042: IKR evidence requires a negated annotation."""

import pytest
from gorule_cases import annotation, facts, found

from standard_annotation_backend.domain.semantic_validation.gorules.terms import (
    gorule_0000042,
)


@pytest.mark.parametrize("negation", [None, False])
def test_ikr_without_negation_is_an_issue(negation: bool | None) -> None:
    """IKR on an annotation that is not negated is reported."""
    subject = annotation("IKR", negation=negation)

    assert found(gorule_0000042(subject, facts())) == [
        ("gorule_0000042", ("annotation",))
    ]


def test_negated_ikr_and_other_evidence_pass() -> None:
    """A negated IKR annotation, and IDA without negation, pass."""
    assert found(gorule_0000042(annotation("IKR", negation=True), facts())) == []
    assert found(gorule_0000042(annotation("IDA"), facts())) == []
