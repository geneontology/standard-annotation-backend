"""Verify GORULE:0000022: retracted publications cannot be cited."""

from gorule_cases import annotation, facts, found, go_rule

rule = go_rule("gorule_0000022")


def test_a_retracted_reference_is_reported_at_that_reference() -> None:
    """Each retracted PMID is located at its reference."""
    subject = annotation(references=["DOI:10.1000/x", "PMID:9"])

    issues = rule(subject, facts(retracted=["PMID:9"]))

    assert found(issues) == [("gorule_0000022", ("annotation", "references", 1))]


def test_references_that_are_not_retracted_pass() -> None:
    """A reference not listed as retracted passes."""
    assert found(rule(annotation(), facts())) == []
