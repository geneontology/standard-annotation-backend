"""Verify GORULE:0000005: binding terms cannot use inferred evidence."""

import pytest
from gorule_cases import annotation, facts, found, go_rule, term

rule = go_rule("gorule_0000005")


@pytest.mark.parametrize("code", ["IEA", "ISS", "ISO", "ISM", "ISA", "IBA", "RCA"])
def test_binding_terms_reject_each_listed_evidence_code(code: str) -> None:
    """Each evidence code in the rule body is reported on `GO:0005515`."""
    subject = annotation(code, ontology_class_id="GO:0005515")

    issues = rule(subject, facts({"GO:0005515": term()}))

    assert found(issues) == [("gorule_0000005", ("annotation",))]


@pytest.mark.parametrize(
    ("term_id", "code"),
    [("GO:0005515", "IDA"), ("GO:0005488", "IPI"), ("GO:0042803", "RCA")],
)
def test_other_evidence_or_terms_pass(term_id: str, code: str) -> None:
    """Other evidence on binding terms, and listed evidence on other terms, pass."""
    subject = annotation(code, ontology_class_id=term_id)

    assert found(rule(subject, facts({term_id: term()}))) == []
