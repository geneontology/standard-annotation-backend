"""Verify GORULE:0000030: GO_REF references are listed, current, and primary."""

from dataclasses import replace

import pytest
from gorule_cases import RULE_DATA, annotation, facts, go_rule

from standard_annotation_backend.domain.semantic_validation.rule_data import (
    GoRefEntry,
)

rule = go_rule("gorule_0000030")

DATA = replace(
    RULE_DATA,
    go_refs=(
        GoRefEntry("GO_REF:0000033"),
        GoRefEntry("GO_REF:0000001", obsolete=True),
        GoRefEntry("GO_REF:0000002", alt_ids=("GO_REF:0000099",)),
    ),
)


@pytest.mark.parametrize(
    ("reference", "details"),
    [
        ("GO_REF:0000555", None),
        ("GO_REF:0000001", None),
        ("GO_REF:0000099", {"primary_id": "GO_REF:0000002"}),
    ],
)
def test_unlisted_obsolete_and_alternative_references_are_issues(
    reference: str, details: dict[str, object] | None
) -> None:
    """Each kind of problem is located at the reference; an alt ID names its primary."""
    subject = annotation(references=["PMID:1", reference])

    issues = list(rule(subject, facts(rule_data=DATA)))

    assert [
        (issue["type"], issue["location"], issue.get("details")) for issue in issues
    ] == [("gorule_0000030", ("annotation", "references", 1), details)]


def test_a_current_listed_reference_passes() -> None:
    """A listed, current GO_REF passes, and nothing fires without rule data."""
    subject = annotation(references=["GO_REF:0000033"])
    unlisted = annotation(references=["GO_REF:0000555"])

    assert list(rule(subject, facts(rule_data=DATA))) == []
    assert list(rule(unlisted, facts(rule_data=None))) == []
