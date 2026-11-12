"""Verify GORULE:0000063: sequence similarity evidence names its source."""

import pytest
from gorule_cases import annotation, facts, found

from standard_annotation_backend.domain.semantic_validation.gorules.support import (
    gorule_0000063,
)


@pytest.mark.parametrize("code", ["ISS", "ISA", "ISO"])
def test_similarity_without_values_is_an_issue(code: str) -> None:
    """ISS, ISA, or ISO with no `with_or_from` value is reported."""
    assert found(gorule_0000063(annotation(code), facts())) == [
        ("gorule_0000063", ("annotation",))
    ]


def test_similarity_with_values_and_other_evidence_pass() -> None:
    """ISO with a value passes, and IDA is not checked."""
    iso = annotation("ISO", with_or_from=["UniProtKB:Q1"])

    assert found(gorule_0000063(iso, facts())) == []
    assert found(gorule_0000063(annotation("IDA"), facts())) == []
