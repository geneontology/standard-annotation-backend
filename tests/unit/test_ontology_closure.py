"""Verify predicate-specific minimum-depth ontology closure."""

from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType

from standard_annotation_backend.domain.ontology import (
    OntologyClosureRow,
    OntologyDocument,
    OntologyEdge,
    OntologyKey,
    OntologySnapshot,
    OntologyTerm,
    compute_closure,
)


def _snapshot(
    edges: tuple[OntologyEdge, ...],
    *,
    predicates: tuple[str, ...] | None = None,
) -> OntologySnapshot:
    term_ids = ("GO:1", "GO:2", "GO:3", "GO:4")
    content = b"test ontology"
    return OntologySnapshot(
        document=OntologyDocument(
            ontology_key=OntologyKey.GO,
            content=content,
            source_type="test",
            source_locator="fixture:closure",
            source_revision="a" * 40,
            source_checksum=sha256(content).hexdigest(),
            fetched_at=datetime(2026, 9, 28, tzinfo=UTC),
        ),
        document_version="test",
        closure_predicates=(
            tuple(sorted({edge.predicate_id for edge in edges}))
            if predicates is None
            else predicates
        ),
        terms=MappingProxyType(
            {term_id: OntologyTerm(term_id, False, (), ()) for term_id in term_ids}
        ),
        edges=edges,
    )


def test_closure_uses_shortest_depth_without_mixing_predicates() -> None:
    """A diamond keeps its shortest depth and part-of edges remain separate."""
    snapshot = _snapshot(
        (
            OntologyEdge("GO:2", "rdfs:subClassOf", "GO:1"),
            OntologyEdge("GO:3", "rdfs:subClassOf", "GO:1"),
            OntologyEdge("GO:4", "rdfs:subClassOf", "GO:2"),
            OntologyEdge("GO:4", "rdfs:subClassOf", "GO:3"),
            OntologyEdge("GO:4", "BFO:0000050", "GO:1"),
        )
    )

    rows = tuple(compute_closure(snapshot))

    assert OntologyClosureRow("GO:4", "rdfs:subClassOf", "GO:1", 2) in rows
    assert OntologyClosureRow("GO:4", "BFO:0000050", "GO:1", 1) in rows
    assert OntologyClosureRow("GO:4", "rdfs:subClassOf", "GO:4", 0) in rows
    assert OntologyClosureRow("GO:4", "BFO:0000050", "GO:2", 2) not in rows
    assert len(rows) == len(set(rows))


def test_closure_terminates_on_cycles_with_minimum_depths() -> None:
    """A cyclic source graph cannot loop or replace a term's self depth."""
    snapshot = _snapshot(
        (
            OntologyEdge("GO:1", "rdfs:subClassOf", "GO:2"),
            OntologyEdge("GO:2", "rdfs:subClassOf", "GO:1"),
        )
    )

    rows = tuple(compute_closure(snapshot))

    assert OntologyClosureRow("GO:1", "rdfs:subClassOf", "GO:1", 0) in rows
    assert OntologyClosureRow("GO:1", "rdfs:subClassOf", "GO:2", 1) in rows
    assert OntologyClosureRow("GO:2", "rdfs:subClassOf", "GO:1", 1) in rows
    assert len(rows) == 6


def test_closure_includes_self_rows_for_a_configured_predicate_without_edges() -> None:
    """Exact terms remain discoverable when a loaded predicate has no edges."""
    rows = tuple(compute_closure(_snapshot((), predicates=("rdfs:subClassOf",))))

    assert rows == tuple(
        OntologyClosureRow(term_id, "rdfs:subClassOf", term_id, 0)
        for term_id in ("GO:1", "GO:2", "GO:3", "GO:4")
    )
