"""Verify GORULE:0000015: interacting taxa need an interspecies term and a
taxon other than the entity's own."""

import pytest
from gorule_cases import TERM, annotation, facts, found, go_rule, term

from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    BP_ROOT,
    INTERSPECIES_INTERACTION_TERMS,
)

rule = go_rule("gorule_0000015")


def test_interacting_taxa_on_an_ordinary_term_are_an_issue() -> None:
    """A term under none of the three interspecies terms cannot name interacting taxa."""
    subject = annotation(interacting_taxon_id=["NCBITaxon:10090"])

    assert found(rule(subject, facts())) == [("gorule_0000015", ("annotation",))]


@pytest.mark.parametrize("ancestor", sorted(INTERSPECIES_INTERACTION_TERMS))
def test_terms_under_an_interspecies_term_pass(ancestor: str) -> None:
    """A term under any of the three interspecies terms may name interacting taxa."""
    subject = annotation(interacting_taxon_id=["NCBITaxon:10090"])

    assert found(rule(subject, facts({TERM: term(BP_ROOT, ancestor)}))) == []


def test_the_entitys_own_taxon_is_reported_at_that_taxon() -> None:
    """An interacting taxon equal to the entity's taxon is located at itself."""
    subject = annotation(interacting_taxon_id=["NCBITaxon:10090", "NCBITaxon:7955"])

    issues = rule(subject, facts({TERM: term(BP_ROOT, "GO:0044419")}))

    assert found(issues) == [
        ("gorule_0000015", ("annotation", "interacting_taxon_id", 1))
    ]


def test_an_unknown_entity_skips_only_the_taxon_comparison() -> None:
    """Without the entity's taxon, only the term requirement is checked."""
    subject = annotation(interacting_taxon_id=["NCBITaxon:7955"])

    issues = rule(subject, facts(entity=None))

    assert found(issues) == [("gorule_0000015", ("annotation",))]


def test_no_interacting_taxa_pass() -> None:
    """An annotation without interacting taxa passes."""
    assert found(rule(annotation(), facts())) == []
