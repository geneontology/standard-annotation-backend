"""Test annotation search criteria rules without HTTP or PostgreSQL."""

from dataclasses import fields
from datetime import date

import pytest

from standard_annotation_backend.api.models import AnnotationSearchQuery
from standard_annotation_backend.domain.annotation_search import (
    AnnotationFilter,
    ClosureTermRequiredError,
)


def test_closure_predicate_without_ontology_term_is_rejected() -> None:
    """A closure predicate needs an ontology class to expand."""
    with pytest.raises(ClosureTermRequiredError):
        AnnotationFilter(ontology_class_id_closure="BFO:0000050")


def test_closure_predicate_with_ontology_term_is_accepted() -> None:
    """A closure predicate paired with an ontology class forms valid criteria."""
    criteria = AnnotationFilter(
        ontology_class_id="GO:0008150",
        ontology_class_id_closure="BFO:0000050",
    )

    assert criteria.ontology_class_id == "GO:0008150"
    assert criteria.ontology_class_id_closure == "BFO:0000050"


def test_every_query_filter_reaches_the_search_criteria() -> None:
    """Each documented filter maps to the same-named criterion, value unchanged.

    A filter added to the query model but not to the criteria (or not copied by
    `to_filter`) would otherwise be accepted and silently ignored.
    """
    assert set(AnnotationSearchQuery.model_fields) == {
        field.name for field in fields(AnnotationFilter)
    }
    query = AnnotationSearchQuery(
        db_object_id="UniProtKB:P12345",
        negation=True,
        relation="RO:0002331",
        ontology_class_id="GO:0008150",
        ontology_class_id_closure="BFO:0000050",
        references=["PMID:1", "PMID:2"],
        evidence_type="ECO:0000314",
        with_or_from=["UniProtKB:Q1"],
        interacting_taxon_id=["NCBITaxon:9606"],
        annotation_date=date(2026, 10, 7),
        assigned_by="MGI",
    )

    assert query.to_filter() == AnnotationFilter(
        db_object_id="UniProtKB:P12345",
        negation=True,
        relation="RO:0002331",
        ontology_class_id="GO:0008150",
        ontology_class_id_closure="BFO:0000050",
        references=("PMID:1", "PMID:2"),
        evidence_type="ECO:0000314",
        with_or_from=("UniProtKB:Q1",),
        interacting_taxon_id=("NCBITaxon:9606",),
        annotation_date=date(2026, 10, 7),
        assigned_by="MGI",
    )
