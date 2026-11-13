"""Verify GORULE:0000017: IDA evidence has no `with_or_from` values."""

from gorule_cases import annotation, facts, found, go_rule

rule = go_rule("gorule_0000017")


def test_ida_with_values_is_an_issue() -> None:
    """IDA with a `with_or_from` value is reported."""
    subject = annotation("IDA", with_or_from=["UniProtKB:Q1"])

    assert found(rule(subject, facts())) == [("gorule_0000017", ("annotation",))]


def test_ida_without_values_and_other_evidence_pass() -> None:
    """IDA without values, and IMP with values, pass."""
    assert found(rule(annotation("IDA"), facts())) == []
    imp = annotation("IMP", with_or_from=["UniProtKB:Q1"])
    assert found(rule(imp, facts())) == []
