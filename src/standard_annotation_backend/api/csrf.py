"""Protect cookie-authenticated mutations with a signed double-submit cookie.

The design is based on OWASP's session-bound signed double-submit cookie pattern:
https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html#signed-double-submit-cookie-recommended
"""

from hashlib import sha256
from hmac import compare_digest, new

from fastapi import Request

from standard_annotation_backend.domain.errors import ForbiddenError

MANAGEMENT_CSRF_COOKIE = "sab_token_management_csrf"
MANAGEMENT_CSRF_HEADER = "X-CSRF-Token"
MANAGEMENT_SESSION_COOKIE = "sab_token_management_session"
_CSRF_PREFIX = "sab_csrf_"


class CsrfValidationError(ForbiddenError):
    """Indicate that a token-management mutation lacks matching CSRF values."""

    code = "invalid_csrf_token"
    message = "Token-management request could not be verified"


def management_csrf_token(raw_session: str, application_secret: str) -> str:
    """Derive a CSRF value bound to one management-session credential."""
    digest = new(
        application_secret.encode(),
        raw_session.encode(),
        sha256,
    ).hexdigest()
    return _CSRF_PREFIX + digest


def require_management_csrf(request: Request) -> None:
    """Require the browser's CSRF cookie value in a request header.

    Args:
        request: Cookie-authenticated token-management request.

    Raises:
        CsrfValidationError: If either value is absent or the values differ.
    """
    raw_session = request.cookies.get(MANAGEMENT_SESSION_COOKIE)
    cookie_value = request.cookies.get(MANAGEMENT_CSRF_COOKIE)
    header_value = request.headers.get(MANAGEMENT_CSRF_HEADER)
    if (
        raw_session is None
        or cookie_value is None
        or header_value is None
        or not compare_digest(
            cookie_value,
            management_csrf_token(
                raw_session, request.app.state.settings.application_secret
            ),
        )
        or not compare_digest(cookie_value, header_value)
    ):
        raise CsrfValidationError
