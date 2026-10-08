"""Convert application, routing, and unexpected failures into consistent responses."""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.routing import Match

from standard_annotation_backend.api.models import (
    ApiErrorBody,
    ApiErrorDetails,
    ApiErrorResponse,
    ApiValidationIssue,
    DuplicateAnnotationDetails,
    StaleAnnotationVersionDetails,
    StaleChangeSetDetails,
)
from standard_annotation_backend.domain.auth import (
    AuthenticationRequiredError,
    PermissionDeniedError,
)
from standard_annotation_backend.domain.errors import (
    BadRequestError,
    ConflictError,
    DuplicatePeers,
    ErrorDetails,
    ForbiddenError,
    InternalServerError,
    InvalidInputError,
    MethodNotAllowedError,
    NotFoundError,
    PreconditionFailedError,
    PreconditionRequiredError,
    SabError,
    StaleChangeSet,
    StaleVersion,
    UnauthenticatedError,
    UnavailableError,
    UpstreamError,
)
from standard_annotation_backend.domain.validation import ValidationIssue
from standard_annotation_backend.persistence.repositories.auth import (
    CredentialPersistenceError,
)

logger = logging.getLogger(__name__)


class RouteNotFoundError(NotFoundError):
    """Report a request path that matches no route."""

    code = "route_not_found"
    message = "Route was not found"


class MethodNotAllowedRouteError(MethodNotAllowedError):
    """Report a request method that the matched path does not support."""

    code = "method_not_allowed"
    message = "Method is not allowed"


class UnexpectedError(InternalServerError):
    """Report a failure that SAB does not expect.

    The response names no cause, because unexpected exceptions can carry
    secrets or internal details in their messages.
    """

    code = "internal_error"
    message = "An unexpected error occurred"


class OAuthCallbackFailedError(InternalServerError):
    """Report an unexpected failure while completing GitHub authentication.

    Like `UnexpectedError`, it names no cause, because a failure during the OAuth
    callback may retain OAuth secrets.
    """

    code = "oauth_callback_failed"
    message = "GitHub authentication could not be completed"


BEARER_ERRORS: tuple[type[SabError], ...] = (
    AuthenticationRequiredError,
    PermissionDeniedError,
    CredentialPersistenceError,
    UnexpectedError,
)
"""Errors every bearer-protected route can return.

A route that requires a bearer token can reject it, deny the caller's role or
scope, or fail to reach credential storage, before its own work begins. Like
any route, it can also fail unexpectedly. Pass these to `error_responses`
alongside the route's own errors.
"""


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: ApiErrorDetails | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    response = ApiErrorResponse(
        error=ApiErrorBody(code=code, message=message, details=details)
    )
    return JSONResponse(
        status_code=status_code,
        content=response.model_dump(mode="json", exclude_none=True),
        headers=headers,
    )


def _validation_details(
    errors: Sequence[ValidationIssue],
) -> list[ApiValidationIssue]:
    return [ApiValidationIssue.model_validate(error) for error in errors]


STATUS_BY_BASE: tuple[tuple[type[SabError], int], ...] = (
    (BadRequestError, status.HTTP_400_BAD_REQUEST),
    (UnauthenticatedError, status.HTTP_401_UNAUTHORIZED),
    (ForbiddenError, status.HTTP_403_FORBIDDEN),
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (MethodNotAllowedError, status.HTTP_405_METHOD_NOT_ALLOWED),
    (ConflictError, status.HTTP_409_CONFLICT),
    (PreconditionFailedError, status.HTTP_412_PRECONDITION_FAILED),
    (InvalidInputError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (PreconditionRequiredError, status.HTTP_428_PRECONDITION_REQUIRED),
    (InternalServerError, status.HTTP_500_INTERNAL_SERVER_ERROR),
    (UpstreamError, status.HTTP_502_BAD_GATEWAY),
    (UnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE),
)
"""Map each outcome category to its HTTP status."""

_STATUS_DESCRIPTIONS = {
    status.HTTP_400_BAD_REQUEST: "Bad request",
    status.HTTP_401_UNAUTHORIZED: "Authentication required",
    status.HTTP_403_FORBIDDEN: "Forbidden",
    status.HTTP_404_NOT_FOUND: "Not found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "Method not allowed",
    status.HTTP_409_CONFLICT: "Conflict",
    status.HTTP_412_PRECONDITION_FAILED: "Precondition failed",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "Invalid input",
    status.HTTP_428_PRECONDITION_REQUIRED: "Precondition required",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "Internal server error",
    status.HTTP_502_BAD_GATEWAY: "Upstream service failed",
    status.HTTP_503_SERVICE_UNAVAILABLE: "Service unavailable",
}

_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}


class RequestValidationFailedError(InvalidInputError):
    """Report a request that FastAPI rejected before it reached a route.

    FastAPI checks the body, path, query, and header values against each route's
    declared types and raises its own `RequestValidationError` when they do not
    match. This error carries the same problems in SAB's error form, so those
    failures share the standard response and can be listed in route
    documentation. It lives in the API layer because only FastAPI produces it.

    Attributes:
        errors: The located problems FastAPI reported.
    """

    code = "request_validation_error"
    message = "Request validation failed"

    def __init__(self, errors: tuple[ValidationIssue, ...]) -> None:
        self.errors = errors
        super().__init__()

    def details(self) -> ErrorDetails:
        """Return every problem FastAPI reported."""
        return self.errors


def status_for(error_type: type[SabError]) -> int:
    """Return the HTTP status for an error class.

    Raises:
        TypeError: If the class derives from none of the outcome categories.
    """
    for base, status_code in STATUS_BY_BASE:
        if issubclass(error_type, base):
            return status_code
    raise TypeError(f"{error_type.__name__} has no outcome category")


def _headers_for(error_type: type[SabError]) -> Mapping[str, str] | None:
    """Return HTTP-only headers for an error, such as the bearer challenge."""
    if issubclass(error_type, AuthenticationRequiredError):
        return _BEARER_CHALLENGE
    return None


def _api_details(details: ErrorDetails | None) -> ApiErrorDetails | None:
    """Convert domain error details to the public detail models."""
    match details:
        case None:
            return None
        case DuplicatePeers(peer_ids=peer_ids):
            return DuplicateAnnotationDetails(peer_ids=peer_ids)
        case StaleVersion():
            return StaleAnnotationVersionDetails(
                annotation_id=details.annotation_id,
                expected_version=details.expected_version,
                current_version=details.current_version,
            )
        case StaleChangeSet():
            return StaleChangeSetDetails(
                change_set_id=details.change_set_id,
                annotation_id=details.annotation_id,
                expected_version=details.expected_version,
                current_version=details.current_version,
            )
        case _:
            return _validation_details(details)


def sab_error_response(
    error: SabError, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """Build the error envelope for an application error.

    Args:
        error: Error to report.
        headers: Extra response headers, such as `Allow` for an unsupported
            method. Headers the error itself requires, such as the bearer
            challenge, are kept.

    Returns:
        The JSON error response.
    """
    error_type = type(error)
    merged = {**(headers or {}), **(_headers_for(error_type) or {})}
    return _error_response(
        status_code=status_for(error_type),
        code=error.code,
        message=error.message,
        details=_api_details(error.details()),
        headers=merged or None,
    )


def error_responses(*error_types: type[SabError]) -> dict[int | str, dict[str, Any]]:
    """Describe the error responses a route can return, for OpenAPI.

    Errors are grouped by status. Each description lists the codes that status
    can carry, so the documentation always matches what the handler produces.

    Args:
        error_types: Every application error the route can raise.

    Returns:
        A `responses` mapping for a FastAPI route or router.
    """
    codes_by_status: dict[int, list[str]] = {}
    challenges: set[int] = set()
    for error_type in error_types:
        status_code = status_for(error_type)
        codes = codes_by_status.setdefault(status_code, [])
        if error_type.code not in codes:
            codes.append(error_type.code)
        if _headers_for(error_type):
            challenges.add(status_code)
    responses: dict[int | str, dict[str, Any]] = {}
    for status_code in sorted(codes_by_status):
        listed = ", ".join(f"`{code}`" for code in codes_by_status[status_code])
        entry: dict[str, Any] = {
            "model": ApiErrorResponse,
            "description": f"{_STATUS_DESCRIPTIONS[status_code]}: {listed}.",
        }
        if status_code in challenges:
            entry["headers"] = {
                "WWW-Authenticate": {"schema": {"type": "string", "const": "Bearer"}}
            }
        responses[status_code] = entry
    return responses


_HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


def _allowed_methods(request: Request) -> str:
    """Return the `Allow` header value for the request's path.

    Starlette reports only the methods of the first route that matched the
    path, but SAB often serves one path with several routes, such as `GET` and
    `POST` on `/annotations`. Each route is asked whether it would fully match
    the same path with each standard method, which also works through the
    wrappers FastAPI uses for included routers.

    Returns an empty string when no route can be matched this way, such as for
    a 405 raised inside the mounted static files, whose scope has already been
    rewritten; the caller then keeps whatever headers Starlette supplied.
    """
    routes = request.app.router.routes
    allowed = [
        method
        for method in _HTTP_METHODS
        if any(
            route.matches({**request.scope, "method": method})[0] is Match.FULL
            for route in routes
        )
    ]
    return ", ".join(allowed)


def install_exception_handlers(app: FastAPI) -> None:
    """Register the application's consistent error-response handlers.

    Application errors, request validation failures, and Starlette's routing
    errors all use the same JSON error envelope. A path that matches no route
    becomes `route_not_found`, and an unsupported method becomes
    `method_not_allowed` with an `Allow` header naming every method the path
    supports.

    Args:
        app: FastAPI application that should use the handlers.
    """

    @app.exception_handler(SabError)
    def handle_sab_error(_request: Request, error: SabError) -> JSONResponse:
        return sab_error_response(error)

    @app.exception_handler(RequestValidationError)
    def handle_request_validation_error(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        issues = tuple(
            ValidationIssue(
                location=tuple(detail["loc"]),
                message=detail["msg"],
                type=detail["type"],
            )
            for detail in error.errors()
        )
        return sab_error_response(RequestValidationFailedError(issues))

    @app.exception_handler(HTTPException)
    def handle_http_exception(request: Request, error: HTTPException) -> JSONResponse:
        # Starlette raises these for paths and methods that match no route.
        # SAB itself raises none, so any other status is unexpected.
        if error.status_code == status.HTTP_404_NOT_FOUND:
            return sab_error_response(RouteNotFoundError())
        if error.status_code == status.HTTP_405_METHOD_NOT_ALLOWED:
            allowed = _allowed_methods(request)
            return sab_error_response(
                MethodNotAllowedRouteError(),
                {"Allow": allowed} if allowed else error.headers,
            )
        logger.error(
            "Unexpected HTTP exception: method=%s path=%s status=%s",
            request.method,
            request.url.path,
            error.status_code,
        )
        return sab_error_response(UnexpectedError())
