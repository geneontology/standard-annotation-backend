"""Verify GORULE:0000018: IPI evidence has `with_or_from` values."""

from gorule_cases import annotation, facts, found

from standard_annotation_backend.domain.semantic_validation.gorules.support import (
    gorule_0000018,
)


def test_ipi_without_values_is_an_issue() -> None:
    """IPI with no `with_or_from` value is reported."""
    assert found(gorule_0000018(annotation("IPI"), facts())) == [
        ("gorule_0000018", ("annotation",))
    ]


def test_ipi_with_values_and_other_evidence_pass() -> None:
    """IPI with a value, and IDA without one, pass."""
    ipi = annotation("IPI", with_or_from=["UniProtKB:Q1"])

    assert found(gorule_0000018(ipi, facts())) == []
    assert found(gorule_0000018(annotation("IDA"), facts())) == []
