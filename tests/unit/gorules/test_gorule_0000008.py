"""Verify GORULE:0000008: terms in `gocheck_do_not_annotate` cannot be used."""

from gorule_cases import TERM, annotation, facts, found, term

from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    BP_ROOT,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.semantic_validation.gorules.terms import (
    gorule_0000008,
)


def test_a_do_not_annotate_term_is_reported_at_the_term() -> None:
    """A term in the subset is an issue located at `ontology_class_id`."""
    marked = facts({TERM: term(BP_ROOT, subsets=["gocheck_do_not_annotate"])})

    issues = gorule_0000008(annotation(), marked)

    assert found(issues) == [("gorule_0000008", ("annotation", "ontology_class_id"))]


def test_other_subsets_and_non_current_terms_pass() -> None:
    """Other subsets pass, and an obsolete term is not checked."""
    other = facts({TERM: term(BP_ROOT, subsets=["goslim_generic"])})
    obsolete = facts(
        {
            TERM: TermFact(
                TermStatus.OBSOLETE, subsets=frozenset({"gocheck_do_not_annotate"})
            )
        }
    )

    assert found(gorule_0000008(annotation(), other)) == []
    assert found(gorule_0000008(annotation(), obsolete)) == []
