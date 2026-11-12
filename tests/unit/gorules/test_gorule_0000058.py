"""Verify GORULE:0000058: extensions conform to `extensions-constraints.yaml`."""

from collections.abc import Mapping
from dataclasses import replace

from gorule_cases import RULE_DATA, annotation, facts, found, term

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    BP_ROOT,
    MF_ROOT,
)
from standard_annotation_backend.domain.semantic_validation.facts import TermFact
from standard_annotation_backend.domain.semantic_validation.gorules.metadata import (
    gorule_0000058,
)
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    ExtensionConstraint,
)

OCCURS_IN = "BFO:0000066"
DATA = replace(
    RULE_DATA,
    extension_constraints=(
        ExtensionConstraint(OCCURS_IN, ("CL", "UBERON"), (BP_ROOT,), cardinality=1),
        ExtensionConstraint(OCCURS_IN, ("GO",), (MF_ROOT,)),
    ),
)
KINASE = "GO:0004672"


def _extensions(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"extension_relation": relation, "extension_term": term_id}
        for relation, term_id in pairs
    ]


def _issues(
    subject: Annotation, terms: Mapping[str, TermFact] | None = None
) -> list[tuple[str, tuple[str | int, ...]]]:
    return found(gorule_0000058(subject, facts(terms, rule_data=DATA)))


def test_an_extension_meeting_one_entry_conforms() -> None:
    """A CL `occurs_in` filler on a BP term meets the first entry."""
    subject = annotation(annotation_extensions=_extensions((OCCURS_IN, "CL:0000000")))

    assert _issues(subject) == []


def test_namespace_root_and_relation_must_all_match_one_entry() -> None:
    """An unlisted namespace, a term under no root, or an unlisted relation fails."""
    subject = annotation(
        annotation_extensions=_extensions(
            (OCCURS_IN, "CHEBI:1"),
            (OCCURS_IN, "GO:0005634"),
            ("BFO:0000050", "CL:0000000"),
        )
    )

    assert _issues(subject) == [
        ("gorule_0000058", ("annotation", "annotation_extensions", 0)),
        ("gorule_0000058", ("annotation", "annotation_extensions", 1)),
        ("gorule_0000058", ("annotation", "annotation_extensions", 2)),
    ]


def test_another_entry_for_the_same_relation_can_apply() -> None:
    """On an MF term, a GO filler meets the second entry."""
    subject = annotation(
        ontology_class_id=KINASE,
        relation="RO:0002327",
        annotation_extensions=_extensions((OCCURS_IN, "GO:0005634")),
    )

    assert _issues(subject, {KINASE: term(MF_ROOT)}) == []


def test_cardinality_counts_extensions_with_the_same_relation_and_prefix() -> None:
    """Two prefixes pass a limit of one; the same prefix twice fails at both."""
    mixed = annotation(
        annotation_extensions=_extensions(
            (OCCURS_IN, "CL:0000000"), (OCCURS_IN, "UBERON:0000001")
        )
    )
    repeated = annotation(
        annotation_extensions=_extensions(
            (OCCURS_IN, "CL:0000000"), (OCCURS_IN, "CL:0000001")
        )
    )

    assert _issues(mixed) == []
    assert _issues(repeated) == [
        ("gorule_0000058", ("annotation", "annotation_extensions", 0)),
        ("gorule_0000058", ("annotation", "annotation_extensions", 1)),
    ]


def test_an_unknown_term_or_missing_rule_data_is_not_checked() -> None:
    """The rule needs the annotated term's checked ancestors and the constraints."""
    subject = annotation(annotation_extensions=_extensions((OCCURS_IN, "CHEBI:1")))

    assert _issues(subject, {}) == []
    assert found(gorule_0000058(subject, facts(rule_data=None))) == []
