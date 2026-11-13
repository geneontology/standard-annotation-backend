"""Verify that the annotation validator checks a batch candidate by candidate."""

import pytest
from refresh_helpers import TEST_ONTOLOGIES

from standard_annotation_backend.domain.validation import ValidationStage
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_validator import (
    AnnotationValidator,
    Candidate,
)

CLEAN = {
    "db_object_id": "UniProtKB:P12345",
    "relation": "RO:0002331",
    "ontology_class_id": "GO:0006915",
    "references": ["PMID:1"],
    "evidence_type": "ECO:0000314",
    "annotation_date": "2026-11-12",
    "assigned_by": "MGI",
}


@pytest.fixture(autouse=True)
def _reference(active_annotation_subjects: None) -> None:
    """Load the subjects and reference data that valid annotations need."""


def test_a_mixed_batch_gets_each_candidates_own_stage_and_issues(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Each candidate's result is its own: semantic issues, structural, or clean."""
    unknown_entity = {**CLEAN, "db_object_id": "UniProtKB:NOPE"}
    broken = {**CLEAN, "evidence_type": "nonsense"}

    with unit_of_work_factory() as uow:
        results = AnnotationValidator(TEST_ONTOLOGIES).check(
            uow, [Candidate(unknown_entity), Candidate(broken), Candidate(CLEAN)]
        )

    assert [result.stage for result in results] == [
        ValidationStage.SEMANTIC,
        ValidationStage.STRUCTURAL,
        ValidationStage.SEMANTIC,
    ]
    assert [issue["type"] for issue in results[0].issues] == ["unknown_entity"]
    assert results[1].issues[0]["location"][:2] == ("annotation", "evidence_type")
    assert results[2].issues == ()
