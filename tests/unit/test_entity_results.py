"""Tests for the stored form of entity refresh results."""

from uuid import UUID

import pytest

from standard_annotation_backend.domain.entities import (
    STORED_ENTITY_REFRESH_RESULT,
    EntityRefreshResult,
)
from standard_annotation_backend.domain.stored_json import StoredDataError

_SNAPSHOT = UUID("00000000-0000-0000-0000-000000000001")
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
)
_JOB_RESULT = {
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
}


def test_refresh_job_result_keeps_its_stored_keys() -> None:
    """The job result has exactly these keys, including `warning_count`."""
    assert _RESULT.to_job_result() == _JOB_RESULT


@pytest.mark.parametrize(
    "change",
    [
        {"added_count": "1"},
        {"removed_count": -1},
        {"warnings": "w1"},
        {"snapshot_id": "not-a-uuid"},
    ],
)
def test_corrupt_stored_refresh_result_is_rejected(change: dict[str, object]) -> None:
    """Malformed stored results raise `StoredDataError` instead of being trusted."""
    with pytest.raises(StoredDataError):
        STORED_ENTITY_REFRESH_RESULT.load({**_JOB_RESULT, **change})
