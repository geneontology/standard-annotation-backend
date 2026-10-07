"""Convert expected application failures into consistent HTTP responses."""

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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
    InvalidInputError,
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

BEARER_ERRORS: tuple[type[SabError], ...] = (
    AuthenticationRequiredError,
    PermissionDeniedError,
    CredentialPersistenceError,
)
"""Errors every bearer-protected route can return.

A route that requires a bearer token can reject it, deny the caller's role or
scope, or fail to reach credential storage, before its own work begins. Pass
these to `error_responses` alongside the route's own errors.
"""


class ApiError(RuntimeError):
    """Describe an HTTP error that can be returned safely to API clients.

    Attributes:
        status_code: HTTP status code for the response.
        code: Stable identifier that clients can handle programmatically.
        message: Plain-language explanation suitable for clients and logs.
        details: Optional structured information specific to the error.
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: ApiErrorDetails | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


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
    (ConflictError, status.HTTP_409_CONFLICT),
    (PreconditionFailedError, status.HTTP_412_PRECONDITION_FAILED),
    (InvalidInputError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (PreconditionRequiredError, status.HTTP_428_PRECONDITION_REQUIRED),
    (UpstreamError, status.HTTP_502_BAD_GATEWAY),
    (UnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE),
)
"""Map each outcome category to its HTTP status."""

_STATUS_DESCRIPTIONS = {
    status.HTTP_400_BAD_REQUEST: "Bad request",
    status.HTTP_401_UNAUTHORIZED: "Authentication required",
    status.HTTP_403_FORBIDDEN: "Forbidden",
    status.HTTP_404_NOT_FOUND: "Not found",
    status.HTTP_409_CONFLICT: "Conflict",
    status.HTTP_412_PRECONDITION_FAILED: "Precondition failed",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "Invalid input",
    status.HTTP_428_PRECONDITION_REQUIRED: "Precondition required",
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


def sab_error_response(error: SabError) -> JSONResponse:
    """Build the error envelope for an application error."""
    error_type = type(error)
    return _error_response(
        status_code=status_for(error_type),
        code=error.code,
        message=error.message,
        details=_api_details(error.details()),
        headers=_headers_for(error_type),
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


def oauth_callback_failure_response() -> JSONResponse:
    """Return a safe envelope for an unexpected failure during OAuth completion."""
    return _error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="oauth_callback_failed",
        message="GitHub authentication could not be completed",
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Register the application's consistent error-response handlers.

    Args:
        app: FastAPI application that should use the handlers.
    """

    @app.exception_handler(SabError)
    def handle_sab_error(_request: Request, error: SabError) -> JSONResponse:
        return sab_error_response(error)

    @app.exception_handler(ApiError)
    def handle_api_error(_request: Request, error: ApiError) -> JSONResponse:
        return _error_response(
            status_code=error.status_code,
            code=error.code,
            message=error.message,
            details=error.details,
        )

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
