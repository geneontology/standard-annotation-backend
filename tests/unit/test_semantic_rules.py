"""Verify the semantic checks over gathered annotation facts."""

from uuid import UUID

import pytest
from pydantic import TypeAdapter

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation import rules
from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    BP_ROOT,
    PROTEIN_CONTAINING_COMPLEX,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    AnnotationFacts,
    EntityFact,
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    DatabaseEntry,
    EcoUsageConstraint,
    EntityIsComplex,
    RuleData,
    WithPresence,
    WithPresenceCondition,
)
from standard_annotation_backend.domain.semantic_validation.rules import (
    RULES,
    Rule,
    SemanticCheck,
    validate_semantics,
)
from standard_annotation_backend.domain.validation import ValidationIssue

ENTITY = EntityFact(db_object_taxon_id="NCBITaxon:9606", db_object_type="PR:000000001")
TERM = "GO:0006915"
CURRENT = TermFact(TermStatus.CURRENT, checked_ancestors=frozenset({BP_ROOT}))
PEER = UUID("00000000-0000-0000-0000-000000000001")


def _annotation(**changes: object) -> Annotation:
    """Return a valid annotation with the given fields replaced."""
    return Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:P12345",
            "relation": "RO:0002331",
            "ontology_class_id": TERM,
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
    rule_data: RuleData | None = None,
) -> AnnotationFacts:
    return AnnotationFacts(
        terms={TERM: CURRENT} if terms is None else terms,
        entity=entity,
        duplicate_peer_ids=peers,
        rule_data=rule_data,
    )


def test_every_field_that_may_hold_a_term_is_checked_where_it_appears() -> None:
    """Term issues are located at the value, in field order.

    The class, extension terms, `with_or_from` values, and interacting taxa are
    all checked.
    """
    annotation = _annotation(
        annotation_extensions=[
            {"extension_relation": "BFO:0000066", "extension_term": "CL:0000000"},
        ],
        with_or_from=["UniProtKB:Q1", "GO:0003674"],
        interacting_taxon_id=["NCBITaxon:10090"],
    )
    unknown = TermFact(TermStatus.UNKNOWN)
    terms = dict.fromkeys(
        (TERM, "CL:0000000", "GO:0003674", "NCBITaxon:10090"), unknown
    )

    issues = validate_semantics(annotation, _facts(terms))

    assert [
        issue["location"] for issue in issues if issue["type"] == "unknown_term"
    ] == [
        ("annotation", "ontology_class_id"),
        ("annotation", "annotation_extensions", 0, "extension_term"),
        ("annotation", "with_or_from", 1),
        ("annotation", "interacting_taxon_id", 0),
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
    """Unknown, obsolete, and alternative identifiers are separate issues, by type.

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
            ("annotation", "annotation_extensions", 1, "extension_term"),
            "unknown_term",
            None,
        ),
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
    ]


def test_identifiers_without_a_fact_are_not_checked() -> None:
    """An identifier whose prefix no configured ontology owns is never an issue."""
    annotation = _annotation(evidence_type="ECO:0000353", with_or_from=["UniProtKB:Q1"])

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
    """Entity issues come first, then term issues in field order, then duplicates.

    The interacting taxon on an ordinary BP term also breaks GORULE:0000015, which
    follows the catalog and duplicate checks.
    """
    annotation = _annotation(interacting_taxon_id=["NCBITaxon:1"])
    terms = {TERM: CURRENT, "NCBITaxon:1": TermFact(TermStatus.UNKNOWN)}

    issues = validate_semantics(annotation, _facts(terms, entity=None, peers=(PEER,)))

    assert [issue["type"] for issue in issues] == [
        "unknown_entity",
        "unknown_term",
        "duplicate_annotation",
        "gorule_0000015",
    ]


def test_a_missing_term_fact_suppresses_only_the_rules_that_need_it() -> None:
    """An unknown term gets no closure rules, but IPI still needs a with value."""
    annotation = _annotation(
        ontology_class_id="GO:0004672",
        relation="RO:0002327",
        evidence_type="ECO:0000353",
    )

    issues = validate_semantics(
        annotation, _facts({"GO:0004672": TermFact(TermStatus.UNKNOWN)})
    )

    assert [(issue["location"], issue["type"]) for issue in issues] == [
        (("annotation", "ontology_class_id"), "unknown_term"),
        (("annotation",), "gorule_0000018"),
    ]


def test_rules_compose_by_restriction() -> None:
    """Rules 18 and 43 both report an IPI annotation on a complex.

    The evidence usage constraint requires a PMID and, for a complex, a with
    value. Neither rule excuses the other.
    """
    rule_data = RuleData(
        databases=(DatabaseEntry("UniProtKB"),),
        go_refs=(),
        group_shorthands=("GO_Central",),
        eco_usage_constraints=(
            EcoUsageConstraint(
                "ECO:0000353",
                "PMID:[0-9]+",
                WithPresence.CONDITIONAL,
                WithPresenceCondition(
                    EntityIsComplex(),
                    when_true=WithPresence.MANDATORY,
                    when_false=WithPresence.OPTIONAL,
                ),
            ),
        ),
        extension_constraints=(),
    )
    complex_entity = EntityFact(
        "NCBITaxon:7955",
        "GO:0032991",
        type_checked_ancestors=frozenset({PROTEIN_CONTAINING_COMPLEX}),
    )
    annotation = _annotation(evidence_type="ECO:0000353", references=["DOI:10.1000/x"])

    issues = validate_semantics(
        annotation, _facts(entity=complex_entity, rule_data=rule_data)
    )

    assert [issue["type"] for issue in issues] == [
        "gorule_0000018",
        "gorule_0000043",
        "gorule_0000043",
    ]


def _always(issue_type: str) -> SemanticCheck:
    """Return a check that reports one issue of `issue_type` for any annotation."""

    def check(annotation: Annotation, facts: AnnotationFacts) -> list[ValidationIssue]:
        return [ValidationIssue(location=("annotation",), message="x", type=issue_type)]

    return check


def test_issues_follow_the_registry_order() -> None:
    """Issues of different rules come in the order the rules are registered."""
    annotation = _annotation(
        annotation_extensions=[
            {"extension_relation": "BFO:0000066", "extension_term": "CL:0000001"},
            {"extension_relation": "BFO:0000066", "extension_term": "CL:9999999"},
        ],
    )
    terms = {
        TERM: CURRENT,
        "CL:0000001": TermFact(TermStatus.ALTERNATE, primary_id="CL:0000000"),
        "CL:9999999": TermFact(TermStatus.UNKNOWN),
    }

    issues = validate_semantics(annotation, _facts(terms, entity=None, peers=(PEER,)))

    order = [rule.issue_type for rule in RULES]
    types = [issue["type"] for issue in issues]
    assert types == sorted(types, key=order.index)
    assert types[:4] == [
        "unknown_entity",
        "unknown_term",
        "alternate_term_id",
        "duplicate_annotation",
    ]


def test_a_rule_reporting_another_rules_type_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A check may only report its own rule's issue type."""
    monkeypatch.setattr(
        rules, "RULES", (Rule("unknown_entity", "x", _always("unknown_term")),)
    )

    with pytest.raises(ValueError, match="unknown_term"):
        validate_semantics(_annotation(), _facts())


def test_report_only_rules_run_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule that does not block writes runs only for reports."""
    monkeypatch.setattr(
        rules,
        "RULES",
        (Rule("gorule_0000004", "x", _always("gorule_0000004"), blocks_writes=False),),
    )

    assert validate_semantics(_annotation(), _facts()) == ()
    assert [
        issue["type"]
        for issue in validate_semantics(
            _annotation(), _facts(), include_report_only=True
        )
    ] == ["gorule_0000004"]


@pytest.mark.parametrize(
    ("issue_type", "terms", "peers"),
    [
        (
            "obsolete_term",
            {TERM: TermFact(TermStatus.OBSOLETE, replaced_by=("GO:1",))},
            (),
        ),
        (
            "alternate_term_id",
            {TERM: TermFact(TermStatus.ALTERNATE, primary_id="GO:1")},
            (),
        ),
        ("duplicate_annotation", None, (PEER,)),
    ],
)
def test_details_match_the_shape_each_rule_declares(
    issue_type: str, terms: dict[str, TermFact] | None, peers: tuple[UUID, ...]
) -> None:
    """Each issue's details have exactly the fields of its rule's details shape."""
    (rule,) = [rule for rule in RULES if rule.issue_type == issue_type]
    assert rule.details is not None

    (issue,) = [
        issue
        for issue in validate_semantics(_annotation(), _facts(terms, peers=peers))
        if issue["type"] == issue_type
    ]

    details = TypeAdapter(rule.details).validate_python(issue["details"])
    assert details == issue["details"]
