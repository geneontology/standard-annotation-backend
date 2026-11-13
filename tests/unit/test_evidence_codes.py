"""Verify the GO evidence code that SABE finds for each allowed evidence term."""

import pytest
from go_standard_annotation_schema.datamodel.go_standard_annotation_schema import (
    EvidenceType,
)

from standard_annotation_backend.domain.semantic_validation.evidence import (
    go_evidence_code,
)


def test_every_allowed_evidence_term_has_its_own_go_code() -> None:
    """Each evidence term the schema allows has a GO evidence code of its own."""
    codes = [go_evidence_code(member.value) for member in EvidenceType]

    assert len(set(codes)) == len(EvidenceType)


@pytest.mark.parametrize(
    ("evidence_type", "code"),
    [("ECO:0000353", "IPI"), ("ECO:0000314", "IDA"), ("ECO:0000307", "ND")],
)
def test_go_evidence_code_names_the_code_of_an_allowed_term(
    evidence_type: str, code: str
) -> None:
    """An allowed ECO term maps to the GO evidence code it stands for."""
    assert go_evidence_code(evidence_type) == code


def test_go_evidence_code_rejects_a_term_the_schema_does_not_allow() -> None:
    """A non-default ECO term, which schema validation rejects, has no code."""
    with pytest.raises(ValueError):
        go_evidence_code("ECO:0006051")
