"""Verify GORULE:0000037: IBA comes from GO_Central and cites only GO_REF:0000033."""

import pytest
from gorule_cases import annotation, facts, found

from standard_annotation_backend.domain.semantic_validation.gorules.support import (
    gorule_0000037,
)


@pytest.mark.parametrize(
    ("assigned_by", "references"),
    [
        ("MGI", ["GO_REF:0000033"]),
        ("GO_Central", ["GO_REF:0000033", "PMID:1"]),
        ("GO_Central", ["PMID:1"]),
    ],
)
def test_other_groups_or_references_are_an_issue(
    assigned_by: str, references: list[str]
) -> None:
    """IBA from another group, or with any other reference, is reported."""
    subject = annotation("IBA", assigned_by=assigned_by, references=references)

    assert found(gorule_0000037(subject, facts())) == [
        ("gorule_0000037", ("annotation",))
    ]


def test_go_central_with_the_phylogenetic_reference_passes() -> None:
    """IBA from GO_Central citing exactly GO_REF:0000033 passes; IDA is not checked."""
    iba = annotation("IBA", references=["GO_REF:0000033"])
    ida = annotation("IDA", assigned_by="MGI")

    assert found(gorule_0000037(iba, facts())) == []
    assert found(gorule_0000037(ida, facts())) == []
