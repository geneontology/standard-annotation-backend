"""Define token-management lifetime and expiration rules and their errors."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
)
from standard_annotation_backend.domain.errors import (
    BadRequestError,
    ErrorDetails,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
    UnauthenticatedError,
)

if TYPE_CHECKING:
    from standard_annotation_backend.domain.validation import ValidationIssue

TOKEN_MANAGEMENT_SESSION_TTL = timedelta(minutes=15)


class OAuthStateError(BadRequestError):
    """Reject a callback without the browser's matching OAuth state.

    The state value binds a GitHub sign-in callback to the browser that started
    it, which prevents another site from completing a sign-in on a user's behalf.
    """

    code = "invalid_oauth_state"
    message = "OAuth state is invalid"


class OAuthCallbackError(BadRequestError):
    """Reject a denied or incomplete OAuth callback without reflecting its query."""

    code = "invalid_oauth_callback"
    message = "GitHub authentication was not completed"


class OAuthIdentityNotAllowedError(ForbiddenError):
    """Reject a GitHub login that is not in the synchronized authorization state."""

    code = "github_identity_not_allowed"
    message = "GitHub identity is not authorized for token management"


class ManagementSessionRequiredError(UnauthenticatedError):
    """Require a valid token-management session independently of bearer authority.

    Token management authenticates with a short-lived browser session rather
    than a bearer token, so this is distinct from `AuthenticationRequiredError`.
    """

    code = "management_session_required"
    message = "A current token-management session is required"


class TokenContextNotFoundError(NotFoundError):
    """Hide missing and differently owned authorization contexts equally."""

    code = "token_context_not_found"
    message = "Token authorization context was not found"


class TokenNotFoundError(NotFoundError):
    """Hide missing and differently owned token IDs equally."""

    code = "token_not_found"
    message = "Token was not found"


class InvalidTokenError(InvalidInputError):
    """Report invalid token-creation input without retaining untrusted values.

    Attributes:
        errors: Located problems in the rejected name, context, or expiration.
            They never include the submitted values.
    """

    code = "invalid_token"
    message = "Token name, context, or expiration is invalid"

    def __init__(self, errors: tuple[ValidationIssue, ...]) -> None:
        self.errors = errors
        super().__init__()

    def details(self) -> ErrorDetails:
        """Return the problems found in the rejected input."""
        return self.errors


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


class InvalidExpirationError(ValueError):
    """The requested token expiration time cannot be accepted.

    Raised only for problems a client can cause with the requested expiration
    time. Other `ValueError` failures from `resolve_expiration` indicate a
    server-side defect.
    """


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
        InvalidExpirationError: If the expiry lacks a timezone or is outside the
            allowed range.
        ValueError: If the creation time lacks a timezone, which is a caller bug.
    """
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("creation timestamp must have a timezone")
    created_at = created_at.astimezone(UTC)
    maximum = _one_calendar_year_after(created_at)
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise InvalidExpirationError("expiration timestamp must have a timezone")
    try:
        expires_at = expires_at.astimezone(UTC)
    except OverflowError:
        raise InvalidExpirationError(
            "expiration timestamp is outside the supported range"
        ) from None
    if not created_at < expires_at <= maximum:
        raise InvalidExpirationError(
            "expiration must be in the future and within one calendar year"
        )
    return expires_at
