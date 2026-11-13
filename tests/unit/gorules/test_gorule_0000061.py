"""Verify GORULE:0000061: the relation must suit the term's branch."""

import pytest
from gorule_cases import TERM, annotation, facts, go_rule, term

from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    BP_ROOT,
    CATALYTIC_ACTIVITY,
    CC_ROOT,
    CELLULAR_ANATOMICAL_STRUCTURE,
    MF_ROOT,
    PROTEIN_CONTAINING_COMPLEX,
    VIRION_COMPONENT,
)

rule = go_rule("gorule_0000061")

COMPLEX = "GO:0000786"


@pytest.mark.parametrize(
    ("term_id", "checked_ancestors", "relation"),
    [
        (MF_ROOT, (MF_ROOT,), "RO:0002327"),
        ("GO:0003824", (MF_ROOT, CATALYTIC_ACTIVITY), "RO:0002326"),
        (BP_ROOT, (BP_ROOT,), "RO:0002331"),
        (TERM, (BP_ROOT,), "RO:0004035"),
        (CC_ROOT, (CC_ROOT,), "RO:0002432"),
        ("GO:0005634", (CC_ROOT, CELLULAR_ANATOMICAL_STRUCTURE), "RO:0001025"),
        ("GO:0019012", (CC_ROOT, VIRION_COMPONENT), "RO:0002325"),
        (COMPLEX, (CC_ROOT, PROTEIN_CONTAINING_COMPLEX), "BFO:0000050"),
        (
            COMPLEX,
            (CC_ROOT, PROTEIN_CONTAINING_COMPLEX, CELLULAR_ANATOMICAL_STRUCTURE),
            "RO:0001025",
        ),
    ],
)
def test_allowed_relations_pass(
    term_id: str, checked_ancestors: tuple[str, ...], relation: str
) -> None:
    """Each row's relations pass, and a term under both CC rows gets their union."""
    subject = annotation(ontology_class_id=term_id, relation=relation)

    assert list(rule(subject, facts({term_id: term(*checked_ancestors)}))) == []


@pytest.mark.parametrize(
    ("term_id", "checked_ancestors", "relation", "allowed"),
    [
        (MF_ROOT, (MF_ROOT,), "RO:0002326", ["RO:0002327"]),
        (BP_ROOT, (BP_ROOT,), "RO:0002264", ["RO:0002331"]),
        (CC_ROOT, (CC_ROOT,), "RO:0001025", ["RO:0002432"]),
        (
            TERM,
            (BP_ROOT,),
            "RO:0002327",
            [
                "RO:0002263",
                "RO:0002264",
                "RO:0002331",
                "RO:0004032",
                "RO:0004033",
                "RO:0004034",
                "RO:0004035",
            ],
        ),
        (COMPLEX, (CC_ROOT, PROTEIN_CONTAINING_COMPLEX), "RO:0001025", ["BFO:0000050"]),
        ("GO:0099999", (CC_ROOT,), "RO:0002432", []),
    ],
)
def test_other_relations_are_reported_with_the_allowed_ones(
    term_id: str, checked_ancestors: tuple[str, ...], relation: str, allowed: list[str]
) -> None:
    """A disallowed relation is reported at `relation` with the allowed relations.

    A root uses its own row, and a CC term under neither CC row allows none.
    """
    subject = annotation(ontology_class_id=term_id, relation=relation)

    issues = list(rule(subject, facts({term_id: term(*checked_ancestors)})))

    assert [
        (issue["type"], issue["location"], issue.get("details")) for issue in issues
    ] == [
        ("gorule_0000061", ("annotation", "relation"), {"allowed_relations": allowed})
    ]
