"""Verify token expiration validation at UTC calendar boundaries."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from standard_annotation_backend.domain.tokens import (
    InvalidExpirationError,
    resolve_expiration,
)
from standard_annotation_backend.services.token_service import TokenCreateInput


def _input(expires_at: object) -> TokenCreateInput:
    return TokenCreateInput.model_validate(
        {
            "name": " Notebook ",
            "assignment_id": "11111111-1111-1111-1111-111111111111",
            "expires_at": expires_at,
        }
    )


def test_expiration_accepts_exact_calendar_year_and_normalizes_utc() -> None:
    """An aware timestamp exactly one calendar year ahead is normalized to UTC."""
    data = _input("2029-02-28T07:34:56-05:00")

    expiry = resolve_expiration(
        data.expires_at, datetime(2028, 2, 29, 12, 34, 56, tzinfo=UTC)
    )

    assert data.name == "Notebook"
    assert expiry == datetime(2029, 2, 28, 12, 34, 56, tzinfo=UTC)
    assert expiry.tzinfo is UTC


@pytest.mark.parametrize(
    "expires_at",
    ["2028-02-29T12:34:56Z", "2028-02-28T12:34:56Z", "2029-02-28T12:34:56.000001Z"],
)
def test_expiry_must_be_future_and_no_later_than_calendar_year(
    expires_at: str,
) -> None:
    """Past, equal, and one-microsecond-over-limit expirations are rejected."""
    data = _input(expires_at)

    with pytest.raises(ValueError):
        resolve_expiration(
            data.expires_at, datetime(2028, 2, 29, 12, 34, 56, tzinfo=UTC)
        )


def test_expiration_requires_timezone() -> None:
    """Token creation rejects an expiration timestamp without a timezone."""
    with pytest.raises(ValidationError):
        _input("2029-01-01T00:00:00")


def test_expiration_resolution_requires_timezone_on_creation_time() -> None:
    """Expiration resolution rejects an ambiguous creation timestamp."""
    data = _input("2029-01-01T00:00:00Z")

    with pytest.raises(ValueError) as caught:
        resolve_expiration(data.expires_at, datetime(2028, 1, 1))

    assert not isinstance(caught.value, InvalidExpirationError)


def test_expiration_outside_allowed_range_is_a_client_error() -> None:
    """An expiration in the past is reported as an invalid expiration."""
    data = _input("2027-01-01T00:00:00Z")

    with pytest.raises(InvalidExpirationError):
        resolve_expiration(data.expires_at, datetime(2028, 1, 1, tzinfo=UTC))
