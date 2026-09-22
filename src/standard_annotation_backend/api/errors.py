"""Convert expected application failures into consistent HTTP responses."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

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
from standard_annotation_backend.domain.auth import (
    AuthenticationRequiredError,
    PermissionDeniedError,
)
from standard_annotation_backend.domain.validation import ValidationIssue
from standard_annotation_backend.persistence.repositories import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
    CommentNotFoundError,
    DuplicateAnnotationError,
    InvalidCommentError,
    StaleAnnotationVersionError,
)
from standard_annotation_backend.persistence.repositories.auth import (
    CredentialPersistenceError,
)
from standard_annotation_backend.services.annotation_service import (
    AnnotationHistoryNotFoundError,
    EmptyAnnotationPatchError,
    InvalidAnnotationPayloadError,
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
) -> JSONResponse:
    response = ApiErrorResponse(
        error=ApiErrorBody(code=code, message=message, details=details)
    )
    return JSONResponse(
        status_code=status_code,
        content=response.model_dump(mode="json", exclude_none=True),
    )


def _validation_details(
    errors: Sequence[ValidationIssue],
) -> list[ApiValidationIssue]:
    return [ApiValidationIssue.model_validate(error) for error in errors]


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

    # "Fixed" means the response depends only on the exception type; no value
    # carried by the exception instance affects its status, body, or headers.
    fixed_errors: dict[type[Exception], _FixedErrorResponse] = {
        AuthenticationRequiredError: _FixedErrorResponse(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "Authentication required",
            (("WWW-Authenticate", "Bearer"),),
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
