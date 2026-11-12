"""Verify GORULE:0000022: retracted publications cannot be cited."""

from gorule_cases import annotation, facts, found

from standard_annotation_backend.domain.semantic_validation.gorules.support import (
    gorule_0000022,
)


def test_a_retracted_reference_is_reported_at_that_reference() -> None:
    """Each retracted PMID is located at its reference."""
    subject = annotation(references=["DOI:10.1000/x", "PMID:9"])

    issues = gorule_0000022(subject, facts(retracted=["PMID:9"]))

    assert found(issues) == [("gorule_0000022", ("annotation", "references", 1))]


def test_references_that_are_not_retracted_pass() -> None:
    """A reference not listed as retracted passes."""
    assert found(gorule_0000022(annotation(), facts())) == []
