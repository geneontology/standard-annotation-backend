"""Verify GORULE:0000055: each reference uses a different prefix."""

import pytest
from gorule_cases import annotation, facts, found

from standard_annotation_backend.domain.semantic_validation.gorules.support import (
    gorule_0000055,
)


def test_each_later_reference_with_a_repeated_prefix_is_an_issue() -> None:
    """The second PMID and the second DOI are each reported at themselves."""
    subject = annotation(
        references=["PMID:1", "DOI:10.1000/x", "PMID:2", "DOI:10.1000/y"]
    )

    assert found(gorule_0000055(subject, facts())) == [
        ("gorule_0000055", ("annotation", "references", 2)),
        ("gorule_0000055", ("annotation", "references", 3)),
    ]


@pytest.mark.parametrize(
    "references", [["PMID:1", "DOI:10.1000/x"], ["PMID:1", "PMID:1"], ["PMID:1"]]
)
def test_distinct_prefixes_or_a_repeated_reference_pass(references: list[str]) -> None:
    """References with different prefixes pass, and so does one reference repeated."""
    assert found(gorule_0000055(annotation(references=references), facts())) == []
