"""Tests for the stored forms of entity refresh and retirement results."""

from uuid import UUID

import pytest

from standard_annotation_backend.domain.entities import (
    STORED_ENTITY_REFRESH_RESULT,
    STORED_ENTITY_RETIREMENT_RESULT,
    EntityCatalogRetirementResult,
    EntityRefreshResult,
    EntityRemovalImpact,
)
from standard_annotation_backend.domain.stored_json import StoredDataError

_SNAPSHOT = UUID("00000000-0000-0000-0000-000000000001")
_ANNOTATION = UUID("00000000-0000-0000-0000-000000000002")
_IMPACT = EntityRemovalImpact("UniProtKB:P1", (_ANNOTATION,))
_RESULT = EntityRefreshResult(
    snapshot_id=_SNAPSHOT,
    source_key="uniprot",
    source_type="https",
    source_locator="https://example.org/a.gpi",
    source_revision=None,
    source_checksum="a" * 64,
    source_record_count=3,
    active_identifier_count=2,
    added_count=1,
    retained_count=1,
    removed_count=1,
    warnings=("w1",),
    removal_impacts=(_IMPACT,),
)
# Exactly what `to_job_result` wrote before stored results were validated.
_LEGACY_RESULT = {
    "snapshot_id": str(_SNAPSHOT),
    "source_key": "uniprot",
    "source_type": "https",
    "source_locator": "https://example.org/a.gpi",
    "source_revision": None,
    "source_checksum": "a" * 64,
    "source_record_count": 3,
    "active_identifier_count": 2,
    "added_count": 1,
    "retained_count": 1,
    "removed_count": 1,
    "warnings": ["w1"],
    "warning_count": 1,
    "removal_impacts": [
        {"db_object_id": "UniProtKB:P1", "annotation_ids": [str(_ANNOTATION)]}
    ],
}


def test_refresh_job_result_keeps_its_stored_keys() -> None:
    """The job result has the same keys and values as before, including `warning_count`."""
    assert _RESULT.to_job_result() == _LEGACY_RESULT


def test_stored_refresh_result_loads_back() -> None:
    """A result stored by earlier code loads into an equal result."""
    assert STORED_ENTITY_REFRESH_RESULT.load(_LEGACY_RESULT) == _RESULT


@pytest.mark.parametrize(
    "change",
    [
        {"added_count": "1"},
        {"removed_count": -1},
        {"warnings": "w1"},
        {"removal_impacts": [{"db_object_id": "UniProtKB:P1"}]},
        {"snapshot_id": "not-a-uuid"},
    ],
)
def test_corrupt_stored_refresh_result_is_rejected(change: dict[str, object]) -> None:
    """Malformed stored results raise `StoredDataError` instead of being trusted."""
    with pytest.raises(StoredDataError):
        STORED_ENTITY_REFRESH_RESULT.load({**_LEGACY_RESULT, **change})


def test_retirement_job_results_keep_their_stored_keys() -> None:
    """Retired and not-retired results keep their earlier key sets."""
    retired = EntityCatalogRetirementResult("uniprot", True, _SNAPSHOT, (_IMPACT,))
    assert retired.to_job_result() == {
        "source_key": "uniprot",
        "retired": True,
        "snapshot_id": str(_SNAPSHOT),
        "removed_count": 1,
        "removal_impacts": [
            {"db_object_id": "UniProtKB:P1", "annotation_ids": [str(_ANNOTATION)]}
        ],
    }
    not_retired = EntityCatalogRetirementResult("uniprot", False, None, ())
    assert not_retired.to_job_result() == {"source_key": "uniprot", "retired": False}


def test_stored_retirement_results_load_back() -> None:
    """Both stored retirement forms load into equal results."""
    for result in (
        EntityCatalogRetirementResult("uniprot", True, _SNAPSHOT, (_IMPACT,)),
        EntityCatalogRetirementResult("uniprot", False, None, ()),
    ):
        assert STORED_ENTITY_RETIREMENT_RESULT.load(result.to_job_result()) == result


def test_corrupt_stored_retirement_result_is_rejected() -> None:
    """A retirement result with a non-boolean `retired` raises `StoredDataError`."""
    with pytest.raises(StoredDataError):
        STORED_ENTITY_RETIREMENT_RESULT.load({"source_key": "uniprot", "retired": 1})
