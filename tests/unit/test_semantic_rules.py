"""Verify the semantic checks over gathered annotation facts."""

from uuid import UUID

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.facts import (
    AnnotationFacts,
    EntityFact,
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.semantic_validation.identifiers import (
    term_identifiers,
)
from standard_annotation_backend.domain.semantic_validation.rules import (
    validate_semantics,
)

ENTITY = EntityFact(db_object_taxon_id="NCBITaxon:9606", db_object_type="PR:000000001")
CURRENT = TermFact(TermStatus.CURRENT)
PEER = UUID("00000000-0000-0000-0000-000000000001")


def _annotation(**changes: object) -> Annotation:
    """Return a valid annotation with the given fields replaced."""
    return Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:P12345",
            "relation": "RO:0002331",
            "ontology_class_id": "GO:0008150",
            "references": ["PMID:1"],
            "evidence_type": "ECO:0000314",
            "annotation_date": "2026-11-11",
            "assigned_by": "GO_Central",
            **changes,
        }
    )


def _facts(
    terms: dict[str, TermFact] | None = None,
    *,
    entity: EntityFact | None = ENTITY,
    peers: tuple[UUID, ...] = (),
) -> AnnotationFacts:
    return AnnotationFacts(
        terms={"GO:0008150": CURRENT} if terms is None else terms,
        entity=entity,
        duplicate_peer_ids=peers,
    )


def test_identifiers_are_listed_in_field_order_with_their_locations() -> None:
    """Every field that may hold an ontology term is listed where issues locate it."""
    annotation = _annotation(
        annotation_extensions=[
            {"extension_relation": "BFO:0000066", "extension_term": "CL:0000000"},
        ],
        with_or_from=["UniProtKB:Q1", "GO:0003674"],
        interacting_taxon_id=["NCBITaxon:10090"],
    )

    assert [
        (occurrence.identifier, occurrence.location, occurrence.prefix)
        for occurrence in term_identifiers(annotation)
    ] == [
        ("GO:0008150", ("annotation", "ontology_class_id"), "GO"),
        (
            "CL:0000000",
            ("annotation", "annotation_extensions", 0, "extension_term"),
            "CL",
        ),
        ("UniProtKB:Q1", ("annotation", "with_or_from", 0), "UniProtKB"),
        ("GO:0003674", ("annotation", "with_or_from", 1), "GO"),
        ("NCBITaxon:10090", ("annotation", "interacting_taxon_id", 0), "NCBITaxon"),
    ]


def test_an_annotation_with_current_facts_has_no_issues() -> None:
    """An active entity, current terms, and no peers pass every check."""
    assert validate_semantics(_annotation(), _facts()) == ()


def test_an_inactive_entity_is_reported_at_db_object_id() -> None:
    """A subject that no entity catalog lists is an `unknown_entity` issue."""
    issues = validate_semantics(_annotation(), _facts(entity=None))

    assert [(issue["location"], issue["type"]) for issue in issues] == [
        (("annotation", "db_object_id"), "unknown_entity")
    ]


def test_each_term_problem_is_reported_at_its_identifier_with_details() -> None:
    """Unknown, obsolete, and alternative identifiers are separate issues.

    An obsolete term lists its suggested replacements, and an alternative
    identifier names its primary term.
    """
    annotation = _annotation(
        ontology_class_id="GO:0000002",
        annotation_extensions=[
            {"extension_relation": "BFO:0000066", "extension_term": "CL:0000001"},
            {"extension_relation": "BFO:0000066", "extension_term": "CL:9999999"},
        ],
    )
    terms = {
        "GO:0000002": TermFact(
            TermStatus.OBSOLETE, replaced_by=("GO:0000001",), consider=("GO:0000003",)
        ),
        "CL:0000001": TermFact(TermStatus.ALTERNATE, primary_id="CL:0000000"),
        "CL:9999999": TermFact(TermStatus.UNKNOWN),
    }

    issues = validate_semantics(annotation, _facts(terms))

    assert [
        (issue["location"], issue["type"], issue.get("details")) for issue in issues
    ] == [
        (
            ("annotation", "ontology_class_id"),
            "obsolete_term",
            {"replaced_by": ["GO:0000001"], "consider": ["GO:0000003"]},
        ),
        (
            ("annotation", "annotation_extensions", 0, "extension_term"),
            "alternate_term_id",
            {"primary_id": "CL:0000000"},
        ),
        (
            ("annotation", "annotation_extensions", 1, "extension_term"),
            "unknown_term",
            None,
        ),
    ]


def test_identifiers_without_a_fact_are_not_checked() -> None:
    """An identifier whose prefix no configured ontology owns is never an issue."""
    annotation = _annotation(with_or_from=["UniProtKB:Q1"])

    assert validate_semantics(annotation, _facts()) == ()


def test_duplicate_peers_are_reported_once_for_the_whole_annotation() -> None:
    """Every peer is listed in one `duplicate_annotation` issue."""
    issues = validate_semantics(_annotation(), _facts(peers=(PEER,)))

    assert issues == (
        {
            "location": ("annotation",),
            "message": "Annotation duplicates other active annotations",
            "type": "duplicate_annotation",
            "details": {"peer_ids": [str(PEER)]},
        },
    )


def test_issues_from_every_check_are_returned_in_a_stable_order() -> None:
    """Entity issues come first, then term issues in field order, then duplicates."""
    annotation = _annotation(interacting_taxon_id=["NCBITaxon:1"])
    terms = {"GO:0008150": CURRENT, "NCBITaxon:1": TermFact(TermStatus.UNKNOWN)}

    issues = validate_semantics(annotation, _facts(terms, entity=None, peers=(PEER,)))

    assert [issue["type"] for issue in issues] == [
        "unknown_entity",
        "unknown_term",
        "duplicate_annotation",
    ]
