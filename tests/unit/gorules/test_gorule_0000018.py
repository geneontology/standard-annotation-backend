"""Verify GORULE:0000018: IPI evidence has `with_or_from` values."""

from gorule_cases import annotation, facts, found, go_rule

rule = go_rule("gorule_0000018")


def test_ipi_without_values_is_an_issue() -> None:
    """IPI with no `with_or_from` value is reported."""
    assert found(rule(annotation("IPI"), facts())) == [
        ("gorule_0000018", ("annotation",))
    ]


def test_ipi_with_values_and_other_evidence_pass() -> None:
    """IPI with a value, and IDA without one, pass."""
    ipi = annotation("IPI", with_or_from=["UniProtKB:Q1"])

    assert found(rule(ipi, facts())) == []
    assert found(rule(annotation("IDA"), facts())) == []
