"""Verify how obsolete terms and alternative IDs are replaced."""

from collections.abc import Collection, Mapping
from uuid import UUID

import pytest

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.ontology import OntologyCatalog
from standard_annotation_backend.domain.semantic_validation.facts import (
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.term_replacement import (
    STORED_TERM_REPLACEMENT_RESULT,
    TermReplacementResult,
    replaced_annotation,
    replacement_chain_terms,
    replacement_for,
)

ONTOLOGIES = OntologyCatalog(
    {"go": ("GO",), "cl": ("CL",), "chebi": ("CHEBI",), "ncbitaxon": ("NCBITaxon",)}
)
CURRENT = TermFact(TermStatus.CURRENT)


def _obsolete(*replaced_by: str) -> TermFact:
    return TermFact(TermStatus.OBSOLETE, replaced_by=replaced_by)


def _alternate(primary_id: str) -> TermFact:
    return TermFact(TermStatus.ALTERNATE, primary_id=primary_id)


@pytest.mark.parametrize(
    ("terms", "expected"),
    [
        ({"GO:1": _obsolete("GO:2"), "GO:2": CURRENT}, "GO:2"),
        ({"GO:1": _alternate("GO:2"), "GO:2": CURRENT}, "GO:2"),
        (
            {"GO:1": _obsolete("GO:2"), "GO:2": _obsolete("GO:3"), "GO:3": CURRENT},
            "GO:3",
        ),
        (
            {"GO:1": _alternate("GO:2"), "GO:2": _obsolete("GO:3"), "GO:3": CURRENT},
            "GO:3",
        ),
    ],
)
def test_single_replacements_alt_ids_and_chains_lead_to_a_current_term(
    terms: dict[str, TermFact], expected: str
) -> None:
    """A replaceable identifier is followed to the current term at its chain's end."""
    assert replacement_for("GO:1", terms, ONTOLOGIES) == expected


@pytest.mark.parametrize(
    "terms",
    [
        {"GO:1": CURRENT},
        {"GO:1": _obsolete()},
        {"GO:1": _obsolete("GO:2", "GO:3"), "GO:2": CURRENT, "GO:3": CURRENT},
        {"GO:1": _obsolete("GO:2"), "GO:2": _obsolete()},
        {"GO:1": _obsolete("GO:2"), "GO:2": TermFact(TermStatus.UNKNOWN)},
        {"GO:1": _obsolete("GO:2")},
        {"GO:1": _obsolete("GO:2"), "GO:2": _obsolete("GO:1")},
        {"GO:1": _obsolete("CHEBI:2"), "CHEBI:2": CURRENT},
        {},
    ],
    ids=[
        "already-current",
        "no-replacement",
        "two-replacements",
        "dead-end",
        "unknown-target",
        "target-not-gathered",
        "loop",
        "other-ontology",
        "no-fact",
    ],
)
def test_other_identifiers_are_not_replaced(terms: dict[str, TermFact]) -> None:
    """Current terms, dead ends, loops, and chains leaving the ontology give nothing."""
    assert replacement_for("GO:1", terms, ONTOLOGIES) is None


def test_an_identifier_no_configured_ontology_owns_is_not_replaced() -> None:
    """A prefix with no ontology, such as a GO_REF, is never replaced."""
    terms = {"GO_REF:1": _alternate("GO_REF:2"), "GO_REF:2": CURRENT}

    assert replacement_for("GO_REF:1", terms, ONTOLOGIES) is None


class _Lookup:
    """Answer term lookups from fixed facts and record each call's identifiers."""

    def __init__(self, facts: Mapping[str, TermFact]) -> None:
        self.facts = facts
        self.calls: list[set[str]] = []

    def __call__(self, identifiers: Collection[str]) -> dict[str, TermFact]:
        self.calls.append(set(identifiers))
        return {
            identifier: self.facts[identifier]
            for identifier in identifiers
            if identifier in self.facts
        }


def test_a_replacement_chain_is_read_to_its_end() -> None:
    """Every term on a chain is read, so the chain's current term can be found."""
    lookup = _Lookup(
        {
            "GO:1": _alternate("GO:2"),
            "GO:2": _obsolete("GO:3"),
            "GO:3": CURRENT,
            "GO:9": CURRENT,
        }
    )

    terms = replacement_chain_terms({"GO:1"}, lookup)

    assert terms == {
        "GO:1": _alternate("GO:2"),
        "GO:2": _obsolete("GO:3"),
        "GO:3": CURRENT,
    }
    assert replacement_for("GO:1", terms, ONTOLOGIES) == "GO:3"


def test_an_identifier_the_lookup_leaves_out_ends_its_chain() -> None:
    """A step to an identifier with no fact ends the chain without an error."""
    lookup = _Lookup({"GO:1": _obsolete("XX:2")})

    terms = replacement_chain_terms({"GO:1"}, lookup)

    assert terms == {"GO:1": _obsolete("XX:2")}
    assert lookup.calls == [{"GO:1"}, {"XX:2"}]


def test_a_cycle_of_replacements_ends() -> None:
    """Terms that replace each other are each read once, and reading stops."""
    lookup = _Lookup({"GO:1": _obsolete("GO:2"), "GO:2": _alternate("GO:1")})

    terms = replacement_chain_terms({"GO:1"}, lookup)

    assert set(terms) == {"GO:1", "GO:2"}
    assert lookup.calls == [{"GO:1"}, {"GO:2"}]


def test_each_identifier_is_read_at_most_once() -> None:
    """Chains that meet, including at an identifier with no fact, share one read."""
    lookup = _Lookup(
        {
            "GO:1": _obsolete("XX:9"),
            "GO:2": _obsolete("GO:5"),
            "GO:3": _alternate("GO:5"),
            "GO:5": _obsolete("XX:9"),
        }
    )

    replacement_chain_terms({"GO:1", "GO:2", "GO:3"}, lookup)

    assert lookup.calls == [{"GO:1", "GO:2", "GO:3"}, {"GO:5", "XX:9"}]


def test_no_identifiers_need_no_lookup() -> None:
    """An empty batch reads nothing."""
    lookup = _Lookup({})

    assert replacement_chain_terms(set(), lookup) == {}
    assert lookup.calls == []


def _annotation(**changes: object) -> Annotation:
    return Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:P12345",
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0000001",
            "references": ["PMID:1"],
            "evidence_type": "ECO:0000353",
            "with_or_from": ["UniProtKB:Q1", "CHEBI:0000001"],
            "interacting_taxon_id": ["NCBITaxon:1"],
            "annotation_extensions": [
                {"extension_relation": "BFO:0000050", "extension_term": "CL:0000001"}
            ],
            "annotation_date": "2026-11-12",
            "assigned_by": "GO_Central",
            **changes,
        }
    )


def test_every_field_the_term_checks_cover_is_replaced() -> None:
    """The class, extension terms, `with_or_from` terms, and taxa are all replaced."""
    terms = {
        "GO:0000001": _obsolete("GO:0000002"),
        "GO:0000002": CURRENT,
        "CL:0000001": _alternate("CL:0000002"),
        "CL:0000002": CURRENT,
        "CHEBI:0000001": _obsolete("CHEBI:0000002"),
        "CHEBI:0000002": CURRENT,
        "NCBITaxon:1": _alternate("NCBITaxon:2"),
        "NCBITaxon:2": CURRENT,
    }

    replaced = replaced_annotation(_annotation(), terms, ONTOLOGIES)

    assert replaced is not None
    assert replaced.ontology_class_id == "GO:0000002"
    assert replaced.with_or_from == ["UniProtKB:Q1", "CHEBI:0000002"]
    assert replaced.interacting_taxon_id == ["NCBITaxon:2"]
    assert replaced.annotation_extensions is not None
    assert replaced.annotation_extensions[0].extension_term == "CL:0000002"
    assert replaced.references == ["PMID:1"]


def test_an_annotation_with_nothing_to_replace_gives_none() -> None:
    """When no identifier is replaceable, there is no replaced annotation."""
    terms = {
        "GO:0000001": CURRENT,
        "CL:0000001": CURRENT,
        "CHEBI:0000001": _obsolete(),
        "NCBITaxon:1": CURRENT,
    }

    assert replaced_annotation(_annotation(), terms, ONTOLOGIES) is None


def test_the_stored_result_round_trips() -> None:
    """A result loads back equal from its job-result form."""
    result = TermReplacementResult(
        group_key="MGI",
        annotation_scan_count=4,
        annotation_replaced_count=1,
        annotation_issue_skip_count=1,
        annotation_stale_skip_count=1,
        snapshot_ids={"go": (UUID(int=1),), "rules": (UUID(int=2),)},
    )

    assert STORED_TERM_REPLACEMENT_RESULT.load(result.to_job_result()) == result


def test_a_replacement_already_in_its_list_leaves_one_copy() -> None:
    """Entries that become equal keep the first one's place; other lists keep repeats."""
    annotation = _annotation(
        with_or_from=["CHEBI:0000001", "UniProtKB:Q1", "CHEBI:0000002"],
        interacting_taxon_id=["NCBITaxon:3", "NCBITaxon:3"],
        annotation_extensions=[
            {"extension_relation": "BFO:0000050", "extension_term": "CL:0000002"},
            {"extension_relation": "BFO:0000050", "extension_term": "CL:0000001"},
            {"extension_relation": "BFO:0000066", "extension_term": "CL:0000001"},
        ],
    )
    terms = {
        "GO:0000001": CURRENT,
        "CL:0000001": _alternate("CL:0000002"),
        "CL:0000002": CURRENT,
        "CHEBI:0000001": _obsolete("CHEBI:0000002"),
        "CHEBI:0000002": CURRENT,
        "NCBITaxon:3": CURRENT,
    }

    replaced = replaced_annotation(annotation, terms, ONTOLOGIES)

    assert replaced is not None
    assert replaced.with_or_from == ["CHEBI:0000002", "UniProtKB:Q1"]
    assert replaced.interacting_taxon_id == ["NCBITaxon:3", "NCBITaxon:3"]
    assert replaced.annotation_extensions is not None
    assert [
        (extension.extension_relation, extension.extension_term)
        for extension in replaced.annotation_extensions
    ] == [("BFO:0000050", "CL:0000002"), ("BFO:0000066", "CL:0000002")]
