"""Verify GORULE:0000033: references are PMID, DOI, or GO_REF identifiers."""

import pytest
from gorule_cases import annotation, facts, found, go_rule

rule = go_rule("gorule_0000033")


@pytest.mark.parametrize("reference", ["ISBN:123", "Reactome:R-HSA-1"])
def test_another_prefix_is_reported_at_the_reference(reference: str) -> None:
    """A reference with another prefix is located at itself."""
    subject = annotation(references=["PMID:1", reference])

    assert found(rule(subject, facts())) == [
        ("gorule_0000033", ("annotation", "references", 1))
    ]


@pytest.mark.parametrize("reference", ["PMID:1", "DOI:10.1000/x", "GO_REF:0000033"])
def test_each_allowed_prefix_passes(reference: str) -> None:
    """PMID, DOI, and GO_REF references pass."""
    assert found(rule(annotation(references=[reference]), facts())) == []
