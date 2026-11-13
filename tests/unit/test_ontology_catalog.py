"""Verify how the ontology catalog assigns identifier prefixes to ontologies."""

import pytest

from standard_annotation_backend.domain.ontology import OntologyCatalog


def test_each_prefix_leads_to_the_ontology_that_owns_it() -> None:
    """A prefix leads to its owning ontology, and an unowned prefix leads nowhere."""
    catalog = OntologyCatalog({"uberon": ("UBERON", "UBERON_X"), "go": ("GO",)})

    assert catalog.key_of("GO") == "go"
    assert catalog.key_of("UBERON_X") == "uberon"
    assert catalog.key_of("MGI") is None
    assert catalog.keys == ("go", "uberon")
    assert catalog.prefixes == frozenset({"GO", "UBERON", "UBERON_X"})
    assert catalog.go_prefixes == frozenset({"GO"})


def test_a_catalog_without_go_is_rejected() -> None:
    """GO Rules need GO, so a catalog must include it."""
    with pytest.raises(ValueError, match="GO"):
        OntologyCatalog({"cl": ("CL",)})


def test_a_prefix_owned_by_two_ontologies_is_rejected() -> None:
    """Each identifier prefix belongs to at most one ontology."""
    with pytest.raises(ValueError, match="CL"):
        OntologyCatalog({"go": ("GO",), "cl": ("CL",), "uberon": ("UBERON", "CL")})


def test_a_prefix_repeated_by_one_ontology_is_accepted() -> None:
    """Listing a prefix twice for the same ontology is not a conflict."""
    catalog = OntologyCatalog({"go": ("GO", "GO")})

    assert catalog.key_of("GO") == "go"


def test_changing_the_original_mapping_does_not_change_the_catalog() -> None:
    """A catalog keeps its own copy of the prefixes it was built from."""
    prefixes = {"go": ("GO",)}
    catalog = OntologyCatalog(prefixes)

    prefixes["cl"] = ("CL",)
    prefixes["go"] = ("GO", "GOX")

    assert catalog.keys == ("go",)
    assert catalog.key_of("CL") is None
    assert catalog.key_of("GOX") is None
