"""Provide internal OAuth and bearer-token management operations."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.security import APIKeyCookie

from standard_annotation_backend.api.csrf import (
    MANAGEMENT_CSRF_COOKIE,
    MANAGEMENT_SESSION_COOKIE,
    CsrfValidationError,
    management_csrf_token,
    require_management_csrf,
)
from standard_annotation_backend.api.dependencies import get_unit_of_work_factory
from standard_annotation_backend.api.errors import (
    OAuthCallbackFailedError,
    RequestValidationFailedError,
    error_responses,
)
from standard_annotation_backend.api.models import (
    TokenContextListResponse,
    TokenContextResource,
    TokenCreatedResponse,
    TokenCreateRequest,
    TokenListResponse,
    TokenResource,
)
from standard_annotation_backend.auth.github_oauth import (
    GitHubOAuthClient,
    OAuthConfigurationError,
    OAuthUpstreamError,
)
from standard_annotation_backend.auth.secrets import generate_secret
from standard_annotation_backend.domain.tokens import (
    TOKEN_MANAGEMENT_SESSION_TTL,
    InvalidTokenError,
    ManagementSessionRequiredError,
    OAuthCallbackError,
    OAuthIdentityNotAllowedError,
    OAuthStateError,
    TokenContextNotFoundError,
    TokenNotFoundError,
)
from standard_annotation_backend.persistence.repositories.auth import (
    CredentialPersistenceError,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.token_service import TokenService

OAUTH_STATE_COOKIE = "sab_oauth_state"
OAUTH_STATE_TTL_SECONDS = 600

router = APIRouter(tags=["token-management"], include_in_schema=False)
management_cookie = APIKeyCookie(
    name=MANAGEMENT_SESSION_COOKIE,
    scheme_name="TokenManagementSession",
    auto_error=False,
    description="15-minute OAuth session for managing your own tokens only.",
)

_PRIVATE_HEADERS = {
    "Cache-Control": {
        "description": "Prevents storing credentials or identity in caches.",
        "schema": {"type": "string"},
    },
    "Referrer-Policy": {
        "description": "Prevents OAuth query values appearing in referrers.",
        "schema": {"type": "string"},
    },
}
_REDIRECT_HEADERS = {
    **_PRIVATE_HEADERS,
    "Location": {
        "description": "Next step in the token-management sign-in flow.",
        "schema": {"type": "string"},
    },
    "Set-Cookie": {
        "description": "Sets an HttpOnly, SameSite=Lax cookie, Secure outside development/testing.",
        "schema": {"type": "string"},
    },
}


def secure_cookies(request: Request) -> bool:
    """Require HTTPS cookies outside explicitly local or test environments.

    Args:
        request: Incoming request whose application holds the environment setting.

    Returns:
        Whether token-management cookies must use the `Secure` attribute.
    """
    return request.app.state.settings.environment not in {"development", "testing"}


def get_token_service(
    unit_of_work_factory: Annotated[
        UnitOfWorkFactory, Depends(get_unit_of_work_factory)
    ],
) -> TokenService:
    """Build token-management operations for one request.

    Args:
        unit_of_work_factory: Callable that opens a database transaction.

    Returns:
        Service configured to manage the current user's tokens.
    """
    return TokenService(unit_of_work_factory)


def get_github_oauth_client(request: Request) -> GitHubOAuthClient:
    """Validate OAuth configuration lazily and build the GitHub client.

    Args:
        request: Incoming request whose application holds the OAuth settings.

    Returns:
        Client configured for GitHub's OAuth web flow.

    Raises:
        OAuthConfigurationError: If required OAuth settings are absent or invalid.
    """
    return GitHubOAuthClient(request.app.state.settings)


@router.get(
    "/auth/github/login",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
    responses={
        status.HTTP_302_FOUND: {"headers": _REDIRECT_HEADERS},
        **error_responses(OAuthConfigurationError),
    },
)
def github_login(
    request: Request,
    github: Annotated[GitHubOAuthClient, Depends(get_github_oauth_client)],
) -> RedirectResponse:
    """Start GitHub sign-in for a short-lived token-management session."""
    state, _digest = generate_secret("sab_oauth_")
    response = RedirectResponse(
        github.authorization_url(state), status_code=status.HTTP_302_FOUND
    )
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        max_age=OAUTH_STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=secure_cookies(request),
        path="/",
    )
    return response


@router.get(
    "/auth/github/callback",
    response_class=RedirectResponse,
    status_code=status.HTTP_303_SEE_OTHER,
    responses=error_responses(
        OAuthStateError,
        OAuthCallbackError,
        OAuthIdentityNotAllowedError,
        OAuthUpstreamError,
        OAuthConfigurationError,
        CredentialPersistenceError,
        OAuthCallbackFailedError,
    ),
)
def github_callback(
    request: Request,
    github: Annotated[GitHubOAuthClient, Depends(get_github_oauth_client)],
    service: Annotated[TokenService, Depends(get_token_service)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Verify browser state and GitHub identity before issuing a management cookie."""
    session = service.complete_login(
        github=github,
        code=code,
        state=state,
        expected_state=request.cookies.get(OAUTH_STATE_COOKIE),
        denied=error is not None,
    )
    csrf_token = management_csrf_token(
        session.raw_secret, request.app.state.settings.application_secret
    )
    response = RedirectResponse(
        "/token-management", status_code=status.HTTP_303_SEE_OTHER
    )
    response.set_cookie(
        MANAGEMENT_SESSION_COOKIE,
        session.raw_secret,
        max_age=int(TOKEN_MANAGEMENT_SESSION_TTL.total_seconds()),
        expires=session.expires_at,
        httponly=True,
        samesite="lax",
        secure=secure_cookies(request),
        path="/",
    )
    response.set_cookie(
        MANAGEMENT_CSRF_COOKIE,
        csrf_token,
        max_age=int(TOKEN_MANAGEMENT_SESSION_TTL.total_seconds()),
        expires=session.expires_at,
        httponly=False,
        samesite="strict",
        secure=secure_cookies(request),
        path="/",
    )
    return response


@router.get(
    "/tokens/contexts",
    response_model=TokenContextListResponse,
    responses=error_responses(
        ManagementSessionRequiredError, CredentialPersistenceError
    ),
)
def list_token_contexts(
    raw_session: Annotated[str | None, Depends(management_cookie)],
    service: Annotated[TokenService, Depends(get_token_service)],
) -> TokenContextListResponse:
    """List the current authorization choices owned by the signed-in user."""
    return TokenContextListResponse(
        items=[
            TokenContextResource.from_service(item)
            for item in service.current_contexts(raw_session)
        ]
    )


@router.get(
    "/tokens",
    response_model=TokenListResponse,
    responses=error_responses(
        ManagementSessionRequiredError, CredentialPersistenceError
    ),
)
def list_tokens(
    raw_session: Annotated[str | None, Depends(management_cookie)],
    service: Annotated[TokenService, Depends(get_token_service)],
) -> TokenListResponse:
    """List your current and historical tokens without their secrets or digests."""
    return TokenListResponse(
        items=[
            TokenResource.from_service(item)
            for item in service.list_tokens(raw_session)
        ]
    )


@router.post(
    "/tokens",
    response_model=TokenCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_201_CREATED: {"headers": _PRIVATE_HEADERS},
        **error_responses(
            ManagementSessionRequiredError,
            CsrfValidationError,
            TokenContextNotFoundError,
            InvalidTokenError,
            RequestValidationFailedError,
            CredentialPersistenceError,
        ),
    },
    dependencies=[Depends(require_management_csrf)],
)
def create_token(
    body: TokenCreateRequest,
    raw_session: Annotated[str | None, Depends(management_cookie)],
    service: Annotated[TokenService, Depends(get_token_service)],
) -> TokenCreatedResponse:
    """Create an expiring token and return its bearer secret exactly once."""
    result = service.create_token(raw_session, body)
    return TokenCreatedResponse.from_service(result)


@router.delete(
    "/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(
        ManagementSessionRequiredError,
        CsrfValidationError,
        TokenNotFoundError,
        RequestValidationFailedError,
        CredentialPersistenceError,
    ),
    dependencies=[Depends(require_management_csrf)],
)
def revoke_token(
    token_id: UUID,
    raw_session: Annotated[str | None, Depends(management_cookie)],
    service: Annotated[TokenService, Depends(get_token_service)],
) -> Response:
    """Revoke an owned token without revealing whether another user owns the ID."""
    service.revoke_token(raw_session, token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
