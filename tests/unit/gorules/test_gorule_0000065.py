"""Verify GORULE:0000065: obsoletion candidates cannot be used."""

from gorule_cases import TERM, annotation, facts, found, term

from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    BP_ROOT,
)
from standard_annotation_backend.domain.semantic_validation.gorules.terms import (
    gorule_0000065,
)


def test_an_obsoletion_candidate_is_reported_at_the_term() -> None:
    """A term in `gocheck_obsoletion_candidate` is located at `ontology_class_id`."""
    marked = facts({TERM: term(BP_ROOT, subsets=["gocheck_obsoletion_candidate"])})

    assert found(gorule_0000065(annotation(), marked)) == [
        ("gorule_0000065", ("annotation", "ontology_class_id"))
    ]


def test_a_term_outside_the_subset_passes() -> None:
    """A term in no subset passes."""
    assert found(gorule_0000065(annotation(), facts())) == []
