"""Verify conversion from OBO syntax to SAB ontology data."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from standard_annotation_backend.domain.ontology import (
    OntologyDefinition,
    OntologyDocument,
    OntologyEdge,
    OntologyKey,
    OntologyParseError,
)
from standard_annotation_backend.ontology.obo_parser import parse_obo

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ontology"
GO_DEFINITION = OntologyDefinition(
    key=OntologyKey.GO,
    identifier_prefixes=("GO",),
    closure_predicates=("rdfs:subClassOf", "BFO:0000050"),
)


def _document(name: str, *, content: bytes | None = None) -> OntologyDocument:
    """Build one immutable source document from a checked-in fixture."""
    raw = (FIXTURES / name).read_bytes() if content is None else content
    return OntologyDocument(
        ontology_key=OntologyKey.GO,
        content=raw,
        source_type="test",
        source_locator=f"fixture:{name}",
        source_revision="a" * 40,
        source_checksum=sha256(raw).hexdigest(),
        retrieved_at=datetime(2026, 9, 28, tzinfo=UTC),
    )


def test_parse_obo_extracts_terms_edges_and_replacement_metadata() -> None:
    """The parser extracts terms, approved edges, and replacement metadata."""
    snapshot = parse_obo(_document("minimal-go.obo"), GO_DEFINITION)

    assert snapshot.document_version == "releases/2026-09-28"
    assert snapshot.terms["GO:0000002"].obsolete is True
    assert snapshot.terms["GO:0000002"].replaced_by == ("GO:0000001",)
    assert snapshot.terms["GO:0000006"].consider == ("GO:0000001",)
    assert (
        OntologyEdge(
            subject_term_id="GO:0000003",
            predicate_id="rdfs:subClassOf",
            object_term_id="GO:0000001",
        )
        in snapshot.edges
    )
    assert (
        OntologyEdge(
            subject_term_id="GO:0000004",
            predicate_id="BFO:0000050",
            object_term_id="GO:0000001",
        )
        in snapshot.edges
    )


def test_parse_obo_excludes_relationships_not_approved_for_closure() -> None:
    """A relation outside the configured list cannot enter stored closure data."""
    snapshot = parse_obo(_document("minimal-go.obo"), GO_DEFINITION)

    assert all(edge.predicate_id != "RO:0002211" for edge in snapshot.edges)


def test_parse_obo_rejects_an_undefined_edge_target() -> None:
    """A dangling ontology edge cannot produce a partial snapshot."""
    with pytest.raises(OntologyParseError, match="undefined term GO:9999999"):
        parse_obo(_document("invalid-reference.obo"), GO_DEFINITION)


def test_parse_obo_rejects_an_undefined_replacement_target() -> None:
    """Replacement metadata must point to a term in the same snapshot."""
    content = b"""\
format-version: 1.4
ontology: go

[Term]
id: GO:0000001
name: obsolete
is_obsolete: true
replaced_by: GO:9999999
"""

    with pytest.raises(OntologyParseError, match="undefined term GO:9999999"):
        parse_obo(_document("replacement.obo", content=content), GO_DEFINITION)


def test_parse_obo_translates_third_party_syntax_failures() -> None:
    """Invalid OBO syntax becomes SAB's stable parser exception."""
    content = b"format-version: 1.4\n\n[Term]\nid GO:0000001\n"

    with pytest.raises(OntologyParseError, match="valid OBO"):
        parse_obo(_document("malformed.obo", content=content), GO_DEFINITION)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"format-version: 1.4\nontology: go\n", "contains no terms"),
        (
            b"format-version: 1.4\nontology: cl\n\n[Term]\nid: GO:0000001\n",
            "declares ontology cl",
        ),
        (
            b"format-version: 1.4\nontology: go\n\n[Term]\nid: CL:0000001\n",
            "unexpected identifier CL:0000001",
        ),
        (
            b"format-version: 1.4\nontology: go\n\n[Term]\nid: GO:0000001\n\n[Term]\nid: GO:0000001\n",
            "duplicate term GO:0000001",
        ),
    ],
)
def test_parse_obo_rejects_wrong_or_incomplete_ontology_content(
    content: bytes, message: str
) -> None:
    """Source identity and term frames must match the configured ontology."""
    with pytest.raises(OntologyParseError, match=message):
        parse_obo(_document("identity.obo", content=content), GO_DEFINITION)


def test_parse_obo_rejects_an_obsolete_replacement_target() -> None:
    """Automatic replacement targets must be current terms in the snapshot."""
    content = b"""\
format-version: 1.4
ontology: go

[Term]
id: GO:0000001
is_obsolete: true
replaced_by: GO:0000002

[Term]
id: GO:0000002
is_obsolete: true
"""

    with pytest.raises(OntologyParseError, match="replacement target GO:0000002"):
        parse_obo(_document("obsolete-target.obo", content=content), GO_DEFINITION)
