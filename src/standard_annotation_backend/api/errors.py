"""Convert expected application failures into consistent HTTP responses."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from standard_annotation_backend.api.csrf import CsrfValidationError
from standard_annotation_backend.api.models import (
    ApiErrorBody,
    ApiErrorDetails,
    ApiErrorResponse,
    ApiValidationIssue,
    DuplicateAnnotationDetails,
    StaleAnnotationVersionDetails,
    StaleChangeSetDetails,
)
from standard_annotation_backend.auth.github_oauth import (
    OAuthConfigurationError,
    OAuthUpstreamError,
)
from standard_annotation_backend.domain.annotation_management import (
    GroupSabManagedError,
)
from standard_annotation_backend.domain.auth import (
    AuthenticationRequiredError,
    PermissionDeniedError,
)
from standard_annotation_backend.domain.entities import UnknownDbObjectIdError
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
from standard_annotation_backend.domain.refresh import UnknownSourceError
from standard_annotation_backend.domain.validation import ValidationIssue
from standard_annotation_backend.persistence.repositories import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
    CommentNotFoundError,
    DuplicateAnnotationError,
    InvalidCommentError,
    JobNotFoundError,
    StaleAnnotationVersionError,
)
from standard_annotation_backend.persistence.repositories.auth import (
    CredentialPersistenceError,
)
from standard_annotation_backend.services.annotation_service import (
    AnnotationHistoryNotFoundError,
    ClosureTermRequiredError,
    EmptyAnnotationPatchError,
    InvalidAnnotationPayloadError,
    OntologyUnavailableError,
    UnsupportedClosureFieldError,
    UnsupportedClosurePredicateError,
)
from standard_annotation_backend.services.change_set_service import (
    ChangeSetNotFoundError,
    ChangeSetStateError,
    InvalidChangeSetError,
    StaleChangeSetError,
)
from standard_annotation_backend.services.token_service import (
    InvalidTokenError,
    ManagementSessionRequiredError,
    OAuthCallbackError,
    OAuthIdentityNotAllowedError,
    OAuthStateError,
    TokenContextNotFoundError,
    TokenNotFoundError,
)

BEARER_ERROR_RESPONSES = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ApiErrorResponse,
        "description": "Authentication required",
        "headers": {
            "WWW-Authenticate": {"schema": {"type": "string", "const": "Bearer"}}
        },
    },
    status.HTTP_403_FORBIDDEN: {
        "model": ApiErrorResponse,
        "description": "Permission denied",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ApiErrorResponse,
        "description": "Credential storage is unavailable",
    },
}

ANNOTATION_WRITE_VALIDATION_RESPONSE = {
    "model": ApiErrorResponse,
    "description": (
        "Request or annotation validation failed. An inactive db_object_id returns "
        "unknown_db_object_id with one validation issue at db_object_id."
    ),
}


@dataclass(frozen=True, slots=True)
class _FixedErrorResponse:
    """Describe an error response determined entirely by exception type.

    Attributes:
        status_code: HTTP status code for the response.
        code: Stable machine-readable error identifier.
        message: Plain-language explanation safe for clients.
        headers: Additional response headers as name-value pairs.
    """

    status_code: int
    code: str
    message: str
    headers: tuple[tuple[str, str], ...] = ()


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

    # "Fixed" means the response depends only on the exception type; no value
    # carried by the exception instance affects its status, body, or headers.
    fixed_errors: dict[type[Exception], _FixedErrorResponse] = {
        AuthenticationRequiredError: _FixedErrorResponse(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "Authentication required",
            (("WWW-Authenticate", "Bearer"),),
        ),
        GroupSabManagedError: _FixedErrorResponse(
            status.HTTP_409_CONFLICT,
            "group_sab_managed",
            "The source's group is managed in SAB, so GPAD can no longer "
            "replace its annotations",
        ),
        PermissionDeniedError: _FixedErrorResponse(
            status.HTTP_403_FORBIDDEN,
            "permission_denied",
            "Permission denied",
        ),
        CsrfValidationError: _FixedErrorResponse(
            status.HTTP_403_FORBIDDEN,
            "invalid_csrf_token",
            "Token-management request could not be verified",
        ),
        CredentialPersistenceError: _FixedErrorResponse(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "credential_storage_unavailable",
            "Credential storage is unavailable",
        ),
        OAuthConfigurationError: _FixedErrorResponse(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "oauth_not_configured",
            "GitHub authentication is not configured",
        ),
        OAuthUpstreamError: _FixedErrorResponse(
            status.HTTP_502_BAD_GATEWAY,
            "github_oauth_unavailable",
            "GitHub authentication is unavailable",
        ),
        OAuthStateError: _FixedErrorResponse(
            status.HTTP_400_BAD_REQUEST,
            "invalid_oauth_state",
            "OAuth state is invalid",
        ),
        OAuthCallbackError: _FixedErrorResponse(
            status.HTTP_400_BAD_REQUEST,
            "invalid_oauth_callback",
            "GitHub authentication was not completed",
        ),
        OAuthIdentityNotAllowedError: _FixedErrorResponse(
            status.HTTP_403_FORBIDDEN,
            "github_identity_not_allowed",
            "GitHub identity is not authorized for token management",
        ),
        ManagementSessionRequiredError: _FixedErrorResponse(
            status.HTTP_401_UNAUTHORIZED,
            "management_session_required",
            "A current token-management session is required",
        ),
        TokenContextNotFoundError: _FixedErrorResponse(
            status.HTTP_404_NOT_FOUND,
            "token_context_not_found",
            "Token authorization context was not found",
        ),
        TokenNotFoundError: _FixedErrorResponse(
            status.HTTP_404_NOT_FOUND,
            "token_not_found",
            "Token was not found",
        ),
        InvalidTokenError: _FixedErrorResponse(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_token",
            "Token name, context, or expiration is invalid",
        ),
        ChangeSetNotFoundError: _FixedErrorResponse(
            status.HTTP_404_NOT_FOUND,
            "change_set_not_found",
            "Change set was not found",
        ),
        ChangeSetStateError: _FixedErrorResponse(
            status.HTTP_409_CONFLICT,
            "change_set_not_proposed",
            "Change set is no longer proposed",
        ),
        EmptyAnnotationPatchError: _FixedErrorResponse(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "empty_annotation_patch",
            "Annotation patch must include at least one field",
        ),
        ClosureTermRequiredError: _FixedErrorResponse(
            status.HTTP_400_BAD_REQUEST,
            "closure_term_required",
            "ontology_class_id is required for closure search",
        ),
        UnsupportedClosureFieldError: _FixedErrorResponse(
            status.HTTP_400_BAD_REQUEST,
            "unsupported_closure_field",
            "Closure search is supported only for ontology_class_id",
        ),
        UnsupportedClosurePredicateError: _FixedErrorResponse(
            status.HTTP_400_BAD_REQUEST,
            "unsupported_closure_predicate",
            "Closure predicate is not loaded for the active ontology",
        ),
        OntologyUnavailableError: _FixedErrorResponse(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ontology_unavailable",
            "The ontology required for closure search is unavailable",
        ),
        AnnotationNotFoundError: _FixedErrorResponse(
            status.HTTP_404_NOT_FOUND,
            "annotation_not_found",
            "Annotation was not found",
        ),
        AnnotationDeletedError: _FixedErrorResponse(
            status.HTTP_404_NOT_FOUND,
            "annotation_not_found",
            "Annotation was not found",
        ),
        CommentNotFoundError: _FixedErrorResponse(
            status.HTTP_404_NOT_FOUND,
            "comment_not_found",
            "Comment was not found",
        ),
        InvalidCommentError: _FixedErrorResponse(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_comment",
            "Comment body must contain non-whitespace text",
        ),
        JobNotFoundError: _FixedErrorResponse(
            status.HTTP_404_NOT_FOUND,
            "job_not_found",
            "Job was not found",
        ),
    }

    def fixed_error_handler(
        error_response: _FixedErrorResponse,
    ) -> Callable[[Request, Exception], JSONResponse]:
        def handle_fixed_error(_request: Request, _error: Exception) -> JSONResponse:
            response = _error_response(
                status_code=error_response.status_code,
                code=error_response.code,
                message=error_response.message,
            )
            for name, value in error_response.headers:
                response.headers[name] = value
            return response

        return handle_fixed_error

    for error_type, error_response in fixed_errors.items():
        app.add_exception_handler(error_type, fixed_error_handler(error_response))

    @app.exception_handler(UnknownDbObjectIdError)
    def handle_unknown_db_object_id(
        _request: Request, _error: UnknownDbObjectIdError
    ) -> JSONResponse:
        message = "Annotation db_object_id is not in the active entity catalog"
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="unknown_db_object_id",
            message=message,
            details=[
                ApiValidationIssue(
                    location=("db_object_id",),
                    message=message,
                    type="unknown_db_object_id",
                )
            ],
        )

    @app.exception_handler(UnknownSourceError)
    def handle_unknown_source(
        _request: Request, _error: UnknownSourceError
    ) -> JSONResponse:
        message = "Source is not configured"
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="unknown_source",
            message=message,
            details=[
                ApiValidationIssue(
                    location=("source_key",),
                    message=message,
                    type="unknown_source",
                )
            ],
        )

    @app.exception_handler(InvalidChangeSetError)
    def handle_invalid_change_set(
        _request: Request, error: InvalidChangeSetError
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_change_set",
            message="Change set is invalid",
            details=_validation_details(error.errors),
        )

    @app.exception_handler(StaleChangeSetError)
    def handle_stale_change_set(
        _request: Request, error: StaleChangeSetError
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_409_CONFLICT,
            code="stale_change_set",
            message="Annotation changed after the change set's base version",
            details=StaleChangeSetDetails(
                change_set_id=error.change_set_id,
                annotation_id=error.annotation_id,
                expected_version=error.expected_version,
                current_version=error.current_version,
            ),
        )

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
        details = [
            ApiValidationIssue(
                location=detail["loc"],
                message=detail["msg"],
                type=detail["type"],
            )
            for detail in error.errors()
        ]
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="request_validation_error",
            message="Request validation failed",
            details=details,
        )

    @app.exception_handler(InvalidAnnotationPayloadError)
    def handle_invalid_annotation_payload(
        _request: Request,
        error: InvalidAnnotationPayloadError,
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="invalid_annotation",
            message="Annotation payload is invalid",
            details=_validation_details(error.errors),
        )

    @app.exception_handler(AnnotationHistoryNotFoundError)
    def handle_annotation_history_not_found(
        _request: Request,
        error: AnnotationHistoryNotFoundError,
    ) -> JSONResponse:
        if error.version is None:
            return _error_response(
                status_code=status.HTTP_404_NOT_FOUND,
                code="annotation_not_found",
                message="Annotation was not found",
            )
        return _error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="version_not_found",
            message="Annotation version was not found",
        )

    @app.exception_handler(DuplicateAnnotationError)
    def handle_duplicate_annotation(
        _request: Request,
        error: DuplicateAnnotationError,
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_409_CONFLICT,
            code="duplicate_annotation",
            message="Annotation conflicts with active duplicates",
            details=DuplicateAnnotationDetails(peer_ids=error.peer_ids),
        )

    @app.exception_handler(StaleAnnotationVersionError)
    def handle_stale_annotation_version(
        _request: Request,
        error: StaleAnnotationVersionError,
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            code="stale_annotation_version",
            message="Annotation version does not match If-Match",
            details=StaleAnnotationVersionDetails(
                annotation_id=error.annotation_id,
                expected_version=error.expected_version,
                current_version=error.current_version,
            ),
        )
