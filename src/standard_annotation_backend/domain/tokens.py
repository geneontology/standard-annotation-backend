"""Define token-management lifetime and expiration rules."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
)

TOKEN_MANAGEMENT_SESSION_TTL = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Describe a token without exposing its bearer secret or lookup digest.

    Attributes:
        token_id: Stable identifier for the token.
        user_id: Identifier of the user who owns the token.
        assignment_id: Identifier of the authorization selected at creation.
        name: User-supplied token name.
        created_at: Time when the token was created.
        expires_at: Time after which the token cannot authenticate.
        last_used_at: Most recent successful authentication time, when available.
        revoked_at: Time when the token was revoked, when applicable.
        role: Authorization role copied from the selected assignment.
        scope: Authorization scope copied from the selected assignment.
        group_id: Selected group, or `None` for global scope.
        assignment_is_active: Whether the selected assignment remains active.
    """

    token_id: UUID
    user_id: UUID
    assignment_id: UUID
    name: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    role: AuthorizationRole
    scope: AuthorizationScope
    group_id: str | None
    assignment_is_active: bool


def _one_calendar_year_after(created_at: datetime) -> datetime:
    """Find the matching valid date one calendar year later.

    Args:
        created_at: UTC token creation time.

    Returns:
        The same time one year later, using the last day of February when the
        original date is February 29 and the following year is not a leap year.
    """
    year = created_at.year + 1
    return created_at.replace(
        year=year,
        day=min(created_at.day, monthrange(year, created_at.month)[1]),
    )


def resolve_expiration(expires_at: datetime, created_at: datetime) -> datetime:
    """Resolve a future UTC expiration no later than one calendar year from creation.

    The maximum expiration clamps to the last valid day of the target month
    while preserving the creation time of day.

    Args:
        expires_at: Requested expiration time with a UTC offset.
        created_at: Token creation time with a UTC offset.

    Returns:
        The requested expiration converted to UTC.

    Raises:
        ValueError: If creation lacks a timezone or expiry is outside the allowed range.
    """
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("creation timestamp must have a timezone")
    created_at = created_at.astimezone(UTC)
    maximum = _one_calendar_year_after(created_at)
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("expiration timestamp must have a timezone")
    try:
        expires_at = expires_at.astimezone(UTC)
    except OverflowError:
        raise ValueError(
            "expiration timestamp is outside the supported range"
        ) from None
    if not created_at < expires_at <= maximum:
        raise ValueError(
            "expiration must be in the future and within one calendar year"
        )
    return expires_at
