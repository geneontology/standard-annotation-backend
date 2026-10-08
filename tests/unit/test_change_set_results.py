"""Tests for the stored form of change-set previews."""

from uuid import UUID

import pytest

from standard_annotation_backend.domain.change_sets import ChangeSetOperation
from standard_annotation_backend.domain.stored_json import StoredDataError
from standard_annotation_backend.services.change_set_service import (
    STORED_CHANGE_SET_PREVIEW,
    ChangeSetPreview,
)

_PREVIEW = ChangeSetPreview(
    change_set_id=UUID("00000000-0000-0000-0000-000000000007"),
    operation=ChangeSetOperation.UPDATE,
    annotation_id=UUID("00000000-0000-0000-0000-000000000008"),
    base_version=2,
    current_version=3,
    before_annotation={"db_object_id": "UniProtKB:P1"},
    annotation={"db_object_id": "UniProtKB:P2", "with_or_from": ["a"]},
    validation_errors=(
        {
            "location": ("annotation", "with_or_from", 0),
            "message": "bad value",
            "type": "value_error",
        },
    ),
    duplicate_peer_ids=(UUID("00000000-0000-0000-0000-000000000009"),),
    is_stale=True,
    can_accept=False,
)

_LEGACY_STORED = {
    "change_set_id": "00000000-0000-0000-0000-000000000007",
    "operation": "update",
    "annotation_id": "00000000-0000-0000-0000-000000000008",
    "base_version": 2,
    "current_version": 3,
    "before_annotation": {"db_object_id": "UniProtKB:P1"},
    "annotation": {"db_object_id": "UniProtKB:P2", "with_or_from": ["a"]},
    "is_deleted": False,
    "validation_errors": [
        {
            "location": ["annotation", "with_or_from", 0],
            "message": "bad value",
            "type": "value_error",
        }
    ],
    "duplicate_peer_ids": ["00000000-0000-0000-0000-000000000009"],
    "is_stale": True,
    "can_accept": False,
}


def test_preview_round_trips_including_mixed_issue_locations() -> None:
    """A stored preview loads back equal, keeping integer location parts."""
    loaded = STORED_CHANGE_SET_PREVIEW.load(STORED_CHANGE_SET_PREVIEW.dump(_PREVIEW))
    assert loaded == _PREVIEW
    assert loaded.validation_errors[0]["location"] == ("annotation", "with_or_from", 0)


def test_preview_stored_form_matches_the_earlier_model_dump() -> None:
    """The stored keys and values are the ones the Pydantic model used to write."""
    assert STORED_CHANGE_SET_PREVIEW.dump(_PREVIEW) == _LEGACY_STORED


def test_preview_stored_by_earlier_code_still_loads() -> None:
    """A preview written before this change loads into the same value."""
    assert STORED_CHANGE_SET_PREVIEW.load(_LEGACY_STORED) == _PREVIEW


def test_corrupt_preview_is_rejected() -> None:
    """A stored preview without `can_accept` raises `StoredDataError`."""
    stored = STORED_CHANGE_SET_PREVIEW.dump(_PREVIEW)
    del stored["can_accept"]
    with pytest.raises(StoredDataError):
        STORED_CHANGE_SET_PREVIEW.load(stored)
