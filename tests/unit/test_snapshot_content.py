"""Verify when two fetched documents count as the same reference data content."""

from datetime import UTC, datetime

from standard_annotation_backend.domain.refresh import RefreshKindName, SourceProvenance
from standard_annotation_backend.domain.snapshots import SourceContent


def _provenance(revision: str | None, checksum: str = "a" * 64) -> SourceProvenance:
    return SourceProvenance(
        source_type="github",
        source_locator="github:geneontology/go-ontology:src/ontology/go-edit.obo",
        source_revision=revision,
        source_checksum=checksum,
        fetched_at=datetime(2026, 11, 13, tzinfo=UTC),
    )


def test_ontology_definitions_compare_without_regard_to_order() -> None:
    """Prefixes and predicates listed in another order are the same definition."""
    first = SourceContent.of(
        RefreshKindName.ONTOLOGY,
        _provenance(None),
        identifier_prefixes=("GO", "GOCHE"),
        closure_predicates=("rdfs:subClassOf", "BFO:0000050"),
    )
    second = SourceContent.of(
        RefreshKindName.ONTOLOGY,
        _provenance(None),
        identifier_prefixes=("GOCHE", "GO"),
        closure_predicates=("BFO:0000050", "rdfs:subClassOf"),
    )

    assert first == second
