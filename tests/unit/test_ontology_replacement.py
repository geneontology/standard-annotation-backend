"""Verify safe annotation proposals for ontology term replacement."""

from datetime import UTC, datetime

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.ontology import (
    OntologyDefinition,
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
    OntologyTerm,
    propose_term_replacements,
)

DEFINITION = OntologyDefinition(OntologyKey.GO, ("GO",), ("is_a",))


def _annotation(**changes: object) -> Annotation:
    data: dict[str, object] = {
        "db_object_id": "UniProtKB:P12345",
        "negation": None,
        "relation": "RO:0002331",
        "ontology_class_id": "GO:0000002",
        "references": ["PMID:1"],
        "evidence_type": "ECO:0000314",
        "with_or_from": [],
        "interacting_taxon_id": [],
        "annotation_date": "2026-09-28",
        "assigned_by": "GO_Central",
        "annotation_extensions": [
            {"extension_relation": "BFO:0000050", "extension_term": "GO:0000002"},
            {"extension_relation": "BFO:0000050", "extension_term": "CL:0000000"},
        ],
        "annotation_properties": {"comment": ["keep"]},
    }
    data.update(changes)
    return Annotation.model_validate(data)


def _snapshot(terms: dict[str, OntologyTerm]) -> OntologySnapshot:
    document = OntologyDocument(
        ontology_key=OntologyKey.GO,
        content=b"test",
        source_type="test",
        source_locator="fixture",
        source_revision="revision",
        source_checksum="a" * 64,
        retrieved_at=datetime(2026, 9, 28, tzinfo=UTC),
    )
    return OntologySnapshot(document, "2026-09-28", ("is_a",), terms, ())


def _term(
    term_id: str,
    *,
    obsolete: bool = False,
    replaced_by: tuple[str, ...] = (),
    consider: tuple[str, ...] = (),
) -> OntologyTerm:
    return OntologyTerm(term_id, obsolete, replaced_by, consider)


def test_replaces_primary_and_each_go_extension_in_one_proposal() -> None:
    """Every safe GO occurrence changes while unrelated fields remain intact."""
    snapshot = _snapshot(
        {
            "GO:0000001": _term("GO:0000001"),
            "GO:0000002": _term(
                "GO:0000002", obsolete=True, replaced_by=("GO:0000001",)
            ),
        }
    )
    original = _annotation()

    proposal = propose_term_replacements(original, snapshot, {}, DEFINITION)

    assert proposal.annotation.ontology_class_id == "GO:0000001"
    assert proposal.annotation.annotation_extensions is not None
    assert proposal.annotation.annotation_extensions[0].extension_term == "GO:0000001"
    assert proposal.annotation.annotation_extensions[1].extension_term == "CL:0000000"
    assert proposal.annotation.relation == original.relation
    assert proposal.annotation.evidence_type == original.evidence_type
    assert proposal.annotation.annotation_properties == original.annotation_properties
    assert proposal.changed_paths == (
        "/ontology_class_id",
        "/annotation_extensions/0/extension_term",
    )
    assert proposal.findings == ()


def test_reports_each_unresolved_term_without_choosing_an_alternative() -> None:
    """Ambiguous, consider-only, and replacement-free obsolete terms stay unchanged."""
    snapshot = _snapshot(
        {
            "GO:1": _term("GO:1"),
            "GO:2": _term("GO:2"),
            "GO:10": _term("GO:10", obsolete=True),
            "GO:11": _term("GO:11", obsolete=True, consider=("GO:1",)),
            "GO:12": _term("GO:12", obsolete=True, replaced_by=("GO:1", "GO:2")),
        }
    )
    annotation = _annotation(
        ontology_class_id="GO:10",
        annotation_extensions=[
            {"extension_relation": "BFO:0000050", "extension_term": "GO:11"},
            {"extension_relation": "BFO:0000050", "extension_term": "GO:12"},
        ],
    )

    proposal = propose_term_replacements(annotation, snapshot, {}, DEFINITION)

    assert proposal.annotation == annotation
    assert proposal.changed_paths == ()
    assert [(finding.code, finding.field_paths) for finding in proposal.findings] == [
        ("obsolete_without_replacement", ("/ontology_class_id",)),
        ("consider_only", ("/annotation_extensions/0/extension_term",)),
        ("ambiguous_replacement", ("/annotation_extensions/1/extension_term",)),
    ]
    assert proposal.findings[1].replacement_ids == ("GO:1",)
    assert proposal.findings[2].replacement_ids == ("GO:1", "GO:2")


def test_reports_removed_go_terms_but_ignores_non_go_fillers() -> None:
    """Missing GO identifiers are findings while foreign ontology values are ignored."""
    snapshot = _snapshot({"GO:1": _term("GO:1")})
    annotation = _annotation(
        ontology_class_id="GO:404",
        annotation_extensions=[
            {"extension_relation": "BFO:0000050", "extension_term": "OLD:1"},
            {"extension_relation": "BFO:0000050", "extension_term": "CL:0000000"},
        ],
    )
    previous = {"OLD:1": _term("OLD:1")}

    proposal = propose_term_replacements(annotation, snapshot, previous, DEFINITION)

    assert proposal.annotation == annotation
    assert [
        (finding.term_id, finding.field_paths) for finding in proposal.findings
    ] == [
        ("GO:404", ("/ontology_class_id",)),
        ("OLD:1", ("/annotation_extensions/0/extension_term",)),
    ]
    assert {finding.code for finding in proposal.findings} == {"removed_term"}


def test_groups_repeated_unresolved_term_paths_in_one_finding() -> None:
    """One term finding lists every affected occurrence in stable path order."""
    snapshot = _snapshot({"GO:2": _term("GO:2", obsolete=True, consider=("GO:1",))})

    proposal = propose_term_replacements(_annotation(), snapshot, {}, DEFINITION)

    assert len(proposal.findings) == 1
    assert proposal.findings[0].field_paths == (
        "/ontology_class_id",
        "/annotation_extensions/0/extension_term",
    )
