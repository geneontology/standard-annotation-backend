"""Verify GORULE:0000043: evidence usage constraints from go-site."""

from dataclasses import replace

import pytest
from gorule_cases import ECO, ENTITY, RULE_DATA, annotation, facts, found, go_rule

from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    PROTEIN_CONTAINING_COMPLEX,
)
from standard_annotation_backend.domain.semantic_validation.facts import EntityFact
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    EcoUsageConstraint,
    EntityIsComplex,
    ReferenceMatches,
    RuleData,
    WithPresence,
    WithPresenceCondition,
)

rule = go_rule("gorule_0000043")

COMPLEX = EntityFact(
    "NCBITaxon:7955",
    "GO:0032991",
    type_checked_ancestors=frozenset({PROTEIN_CONTAINING_COMPLEX}),
)


def _data(constraint: EcoUsageConstraint) -> RuleData:
    return replace(RULE_DATA, eco_usage_constraints=(constraint,))


def _ipi(
    allowed_reference: str | None = None,
    presence: WithPresence = WithPresence.OPTIONAL,
    condition: WithPresenceCondition | None = None,
) -> RuleData:
    return _data(EcoUsageConstraint(ECO["IPI"], allowed_reference, presence, condition))


@pytest.mark.parametrize(
    "references",
    [["DOI:10.1000/x"], ["PMID:5x"], ["GO_REF:0000033x"]],
)
def test_no_reference_matching_the_whole_pattern_is_an_issue(
    references: list[str],
) -> None:
    """IPI is reported when no reference matches the whole allowed pattern.

    A reference that only starts with a match, such as `PMID:5x`, does not count.
    """
    data = _ipi("(GO_REF:0000(033|047))|(PMID:[0-9]+)")
    subject = annotation("IPI", references=references, with_or_from=["UniProtKB:Q1"])

    assert found(rule(subject, facts(rule_data=data))) == [
        ("gorule_0000043", ("annotation",))
    ]


@pytest.mark.parametrize(
    "references", [["DOI:10.1000/x", "PMID:5"], ["GO_REF:0000047"]]
)
def test_one_matching_reference_is_enough(references: list[str]) -> None:
    """A PMID cited with its DOI passes: only one reference must match."""
    data = _ipi("(GO_REF:0000(033|047))|(PMID:[0-9]+)")
    subject = annotation("IPI", references=references, with_or_from=["UniProtKB:Q1"])

    assert found(rule(subject, facts(rule_data=data))) == []


@pytest.mark.parametrize(
    ("presence", "with_or_from", "reported"),
    [
        (WithPresence.MANDATORY, [], True),
        (WithPresence.MANDATORY, ["UniProtKB:Q1"], False),
        (WithPresence.DISALLOWED, ["UniProtKB:Q1"], True),
        (WithPresence.DISALLOWED, [], False),
        (WithPresence.OPTIONAL, [], False),
        (WithPresence.RECOMMENDED, [], False),
    ],
)
def test_with_presence_requirements(
    presence: WithPresence, with_or_from: list[str], reported: bool
) -> None:
    """Mandatory and disallowed are enforced; optional and recommended are not."""
    subject = annotation("IPI", with_or_from=with_or_from)

    issues = found(rule(subject, facts(rule_data=_ipi(presence=presence))))

    assert issues == ([("gorule_0000043", ("annotation",))] if reported else [])


def test_the_complex_condition_chooses_the_requirement_by_entity_type() -> None:
    """A complex gets `when_true`, another entity `when_false`, an unknown one neither."""
    condition = WithPresenceCondition(
        EntityIsComplex(),
        when_true=WithPresence.DISALLOWED,
        when_false=WithPresence.MANDATORY,
    )
    data = _ipi(presence=WithPresence.CONDITIONAL, condition=condition)
    with_values = annotation("IPI", with_or_from=["UniProtKB:Q1"])
    without = annotation("IPI")
    reported = [("gorule_0000043", ("annotation",))]

    assert found(rule(with_values, facts(entity=COMPLEX, rule_data=data))) == reported
    assert found(rule(without, facts(entity=ENTITY, rule_data=data))) == reported
    assert found(rule(without, facts(entity=None, rule_data=data))) == []


def test_an_unknown_entity_still_gets_the_reference_check() -> None:
    """Only the condition that needs the entity's type is skipped."""
    condition = WithPresenceCondition(
        EntityIsComplex(),
        when_true=WithPresence.MANDATORY,
        when_false=WithPresence.MANDATORY,
    )
    data = _ipi("PMID:[0-9]+", WithPresence.CONDITIONAL, condition)
    subject = annotation("IPI", references=["DOI:10.1000/x"])

    assert found(rule(subject, facts(entity=None, rule_data=data))) == [
        ("gorule_0000043", ("annotation",))
    ]


def test_the_reference_condition_matches_any_whole_reference() -> None:
    """`reference matches` chooses the requirement from the references."""
    condition = WithPresenceCondition(
        ReferenceMatches("^GO_REF:[0-9]{7}$"),
        when_true=WithPresence.MANDATORY,
        when_false=WithPresence.OPTIONAL,
    )
    data = _ipi(presence=WithPresence.CONDITIONAL, condition=condition)
    go_ref = annotation("IPI", references=["GO_REF:0000047"])
    pmid = annotation("IPI", references=["PMID:1"])

    assert found(rule(go_ref, facts(rule_data=data))) == [
        ("gorule_0000043", ("annotation",))
    ]
    assert found(rule(pmid, facts(rule_data=data))) == []


def test_constraints_for_other_evidence_and_missing_rule_data_do_not_apply() -> None:
    """Only the entry for the annotation's own evidence term is used."""
    data = _data(EcoUsageConstraint(ECO["IDA"], None, WithPresence.MANDATORY))

    assert found(rule(annotation("IPI"), facts(rule_data=data))) == []
    assert found(rule(annotation("IDA"), facts(rule_data=None))) == []
