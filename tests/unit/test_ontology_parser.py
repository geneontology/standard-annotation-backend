"""Verify conversion from OBO syntax to SABE ontology data."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from standard_annotation_backend.domain.ontology import (
    GO_ONTOLOGY_KEY,
    OntologyDefinition,
    OntologyDocument,
    OntologyEdge,
    OntologyParseError,
)
from standard_annotation_backend.ontology.obo_parser import parse_obo

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ontology"
GO_DEFINITION = OntologyDefinition(
    key=GO_ONTOLOGY_KEY,
    identifier_prefixes=("GO",),
    closure_predicates=("rdfs:subClassOf", "BFO:0000050"),
    document_ontology_id="go",
)


def _document(name: str, *, content: bytes | None = None) -> OntologyDocument:
    """Build one immutable source document from a checked-in fixture."""
    raw = (FIXTURES / name).read_bytes() if content is None else content
    return OntologyDocument(
        ontology_key=GO_ONTOLOGY_KEY,
        content=raw,
        source_type="test",
        source_locator=f"fixture:{name}",
        source_revision="a" * 40,
        source_checksum=sha256(raw).hexdigest(),
        fetched_at=datetime(2026, 9, 28, tzinfo=UTC),
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


def test_parse_obo_rejects_an_ambiguous_typedef_mapping() -> None:
    """A relation alias cannot select more than one configured predicate."""
    content = b"""\
format-version: 1.4
ontology: go

[Term]
id: GO:0000001
relationship: part_of GO:0000002

[Term]
id: GO:0000002

[Typedef]
id: part_of
xref: BFO:0000050
xref: RO:0002211
"""
    definition = OntologyDefinition(
        key=GO_ONTOLOGY_KEY,
        identifier_prefixes=("GO",),
        closure_predicates=("BFO:0000050", "RO:0002211"),
        document_ontology_id="go",
    )

    with pytest.raises(OntologyParseError, match="typedef part_of maps to multiple"):
        parse_obo(_document("ambiguous-typedef.obo", content=content), definition)


def test_parse_obo_excludes_edge_targets_outside_identifier_prefixes() -> None:
    """Edges to identifiers outside the ontology do not enter closure data."""
    content = b"""\
format-version: 1.4
ontology: go

[Term]
id: GO:0000001
is_a: GO:0000002
is_a: neverin:10090
is_a: onlyin:32524
is_a: external:0000001

[Term]
id: GO:0000002
"""

    snapshot = parse_obo(_document("taxonomy.obo", content=content), GO_DEFINITION)

    assert snapshot.edges == (
        OntologyEdge(
            subject_term_id="GO:0000001",
            predicate_id="rdfs:subClassOf",
            object_term_id="GO:0000002",
        ),
    )


def test_parse_obo_rejects_an_undefined_edge_target() -> None:
    """A dangling ontology edge cannot produce a partial snapshot."""
    with pytest.raises(OntologyParseError, match="undefined term GO:9999999"):
        parse_obo(_document("invalid-reference.obo"), GO_DEFINITION)


def test_parse_obo_retains_an_undefined_consider_target() -> None:
    """Advisory `consider` identifiers need not name terms in the snapshot."""
    content = b"""\
format-version: 1.4
ontology: go

[Term]
id: GO:0000001
is_obsolete: true
consider: GO:9999999
"""

    snapshot = parse_obo(_document("consider.obo", content=content), GO_DEFINITION)

    assert snapshot.terms["GO:0000001"].consider == ("GO:9999999",)


def test_parse_obo_translates_third_party_syntax_failures() -> None:
    """Invalid OBO syntax becomes SABE's stable parser exception."""
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
            "contains no terms",
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


def test_parse_obo_ignores_terms_with_prefixes_the_ontology_does_not_own() -> None:
    """Terms from other ontologies in the same file are not loaded."""
    content = b"""\
format-version: 1.4
ontology: go

[Term]
id: GO:0000001

[Term]
id: CL:0000001
is_a: GO:0000001
"""

    snapshot = parse_obo(_document("foreign.obo", content=content), GO_DEFINITION)

    assert set(snapshot.terms) == {"GO:0000001"}
    assert snapshot.edges == ()


def test_parse_obo_loads_a_file_whose_unread_clauses_are_malformed() -> None:
    """A malformed `def:` or `xref:` does not stop a file from loading.

    `[Typedef]` frames are kept, so relation aliases still map to closure
    predicates.
    """
    content = b"""\
format-version: 1.4
ontology: go

[Term]
id: GO:0000001
def: "A test." [PMID: 1]
xref: http://example.org/wiki/Category\\:Test

[Term]
id: GO:0000002
relationship: part_of GO:0000001

[Typedef]
id: part_of
xref: BFO:0000050
"""

    snapshot = parse_obo(_document("unread.obo", content=content), GO_DEFINITION)

    assert set(snapshot.terms) == {"GO:0000001", "GO:0000002"}
    assert snapshot.edges == (
        OntologyEdge(
            subject_term_id="GO:0000002",
            predicate_id="BFO:0000050",
            object_term_id="GO:0000001",
        ),
    )


def test_parse_obo_keeps_replacement_suggestions_as_published() -> None:
    """A replacement may be obsolete or belong to another ontology."""
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
replaced_by: CL:0000001
"""

    snapshot = parse_obo(_document("chain.obo", content=content), GO_DEFINITION)

    assert snapshot.terms["GO:0000001"].replaced_by == ("GO:0000002",)
    assert snapshot.terms["GO:0000002"].replaced_by == ("CL:0000001",)


def test_parse_obo_records_alternative_identifiers_that_name_one_term() -> None:
    """An alternative identifier is recorded only when it names exactly one term.

    Identifiers with another prefix, identifiers that are also terms, and
    identifiers claimed by two terms are left out.
    """
    content = b"""\
format-version: 1.4
ontology: go

[Term]
id: GO:0000001
alt_id: GO:0000009
alt_id: BS:0000001

[Term]
id: GO:0000002
alt_id: GO:0000003

[Term]
id: GO:0000003

[Term]
id: GO:0000004
alt_id: GO:0000008

[Term]
id: GO:0000005
alt_id: GO:0000008
"""

    snapshot = parse_obo(_document("alt-ids.obo", content=content), GO_DEFINITION)

    assert dict(snapshot.alt_ids) == {"GO:0000009": "GO:0000001"}


def test_parse_obo_reads_term_subsets_and_declared_subsets() -> None:
    """Each term's subsets and the subsets the header declares are read."""
    content = b"""\
format-version: 1.4
ontology: go
subsetdef: gocheck_do_not_annotate "Term not to be used for direct annotation"

[Term]
id: GO:0000001
subset: goslim_generic
subset: gocheck_do_not_annotate

[Term]
id: GO:0000002
"""

    snapshot = parse_obo(_document("subsets.obo", content=content), GO_DEFINITION)

    assert snapshot.terms["GO:0000001"].subsets == (
        "gocheck_do_not_annotate",
        "goslim_generic",
    )
    assert snapshot.terms["GO:0000002"].subsets == ()
    assert snapshot.declared_subsets == frozenset({"gocheck_do_not_annotate"})
