"""Verify GORULE:0000016: IC evidence names the GO term it was inferred from."""

import pytest
from gorule_cases import TERM, annotation, facts, found, go_rule

rule = go_rule("gorule_0000016")


@pytest.mark.parametrize("with_or_from", [[], ["UniProtKB:Q1"], [TERM]])
def test_ic_without_another_go_term_is_an_issue(with_or_from: list[str]) -> None:
    """No GO identifier, or only the annotated term, is reported."""
    subject = annotation("IC", with_or_from=with_or_from)

    assert found(rule(subject, facts())) == [("gorule_0000016", ("annotation",))]


def test_ic_with_another_go_term_and_other_evidence_pass() -> None:
    """IC naming another GO term passes, and other evidence is not checked."""
    inferred = annotation("IC", with_or_from=["UniProtKB:Q1", "GO:0008150"])

    assert found(rule(inferred, facts())) == []
    assert found(rule(annotation("IDA"), facts())) == []
