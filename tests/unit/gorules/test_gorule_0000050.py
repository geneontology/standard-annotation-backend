"""Verify GORULE:0000050: sequence similarity cannot cite the subject itself."""

import pytest
from gorule_cases import annotation, facts, found

from standard_annotation_backend.domain.semantic_validation.gorules.support import (
    gorule_0000050,
)


@pytest.mark.parametrize("code", ["ISS", "ISA", "ISO"])
def test_the_subject_in_with_or_from_is_an_issue(code: str) -> None:
    """Listing `db_object_id` as its own source of similarity is reported."""
    subject = annotation(code, with_or_from=["UniProtKB:Q1", "UniProtKB:P12345"])

    assert found(gorule_0000050(subject, facts())) == [
        ("gorule_0000050", ("annotation",))
    ]


def test_other_sources_and_other_evidence_pass() -> None:
    """Another source passes, and IPI is not checked."""
    iss = annotation("ISS", with_or_from=["UniProtKB:Q1"])
    ipi = annotation("IPI", with_or_from=["UniProtKB:P12345"])

    assert found(gorule_0000050(iss, facts())) == []
    assert found(gorule_0000050(ipi, facts())) == []
