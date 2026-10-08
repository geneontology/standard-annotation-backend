"""Tests for the stored forms of authorization refresh summaries and results."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from standard_annotation_backend.domain.stored_json import StoredDataError
from standard_annotation_backend.services.authorization_refresh_service import (
    STORED_AUTHORIZATION_SUMMARY,
    AuthorizationRefreshResult,
    AuthorizationRefreshSummary,
)


def test_summary_keeps_its_stored_keys() -> None:
    """The stored summary keeps its `users`, `groups`, and `assignments` keys."""
    summary = AuthorizationRefreshSummary(users=3, groups=2, assignments=4)
    stored = {"users": 3, "groups": 2, "assignments": 4}
    assert STORED_AUTHORIZATION_SUMMARY.dump(summary) == stored
    assert STORED_AUTHORIZATION_SUMMARY.load(stored) == summary


@pytest.mark.parametrize(
    "stored",
    [
        {"users": 3, "groups": 2},
        {"users": True, "groups": 2, "assignments": 4},
        {"users": -1, "groups": 2, "assignments": 4},
        {"users": "3", "groups": 2, "assignments": 4},
    ],
)
def test_corrupt_summary_is_rejected(stored: dict[str, object]) -> None:
    """Missing, boolean, negative, or string counts raise `StoredDataError`."""
    with pytest.raises(StoredDataError):
        STORED_AUTHORIZATION_SUMMARY.load(stored)


def test_job_result_keeps_its_keys_with_utc_timestamps() -> None:
    """The job result keeps its keys; timestamps are written in UTC `Z` form."""
    refresh_id = UUID("00000000-0000-0000-0000-000000000006")
    at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    result = AuthorizationRefreshResult(
        refresh_id=refresh_id,
        source_type="github",
        source_locator="github:org/repo:users.yaml",
        source_revision="abc",
        source_checksum="d" * 64,
        fetched_at=at,
        refreshed_at=at,
        user_count=3,
        group_count=2,
        assignment_count=4,
    )
    assert result.to_job_result() == {
        "refresh_id": str(refresh_id),
        "source_type": "github",
        "source_locator": "github:org/repo:users.yaml",
        "source_revision": "abc",
        "source_checksum": "d" * 64,
        "fetched_at": "2026-01-02T03:04:05Z",
        "refreshed_at": "2026-01-02T03:04:05Z",
        "user_count": 3,
        "group_count": 2,
        "assignment_count": 4,
    }
