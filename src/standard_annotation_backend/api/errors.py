"""Convert expected application failures into consistent HTTP responses."""

from collections.abc import Sequence

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
from standard_annotation_backend.domain.validation import ValidationIssue
from standard_annotation_backend.persistence.repositories import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
    DuplicateAnnotationError,
    StaleAnnotationVersionError,
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


def install_exception_handlers(app: FastAPI) -> None:
    """Register the application's consistent error-response handlers.

    Args:
        app: FastAPI application that should use the handlers.
    """

    @app.exception_handler(ChangeSetNotFoundError)
    def handle_change_set_not_found(
        _request: Request, _error: ChangeSetNotFoundError
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="change_set_not_found",
            message="Change set was not found",
        )

    @app.exception_handler(ChangeSetStateError)
    def handle_change_set_state(
        _request: Request, _error: ChangeSetStateError
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_409_CONFLICT,
            code="change_set_not_proposed",
            message="Change set is no longer proposed",
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

    @app.exception_handler(EmptyAnnotationPatchError)
    def handle_empty_annotation_patch(
        _request: Request,
        _error: EmptyAnnotationPatchError,
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="empty_annotation_patch",
            message="Annotation patch must include at least one field",
        )

    @app.exception_handler(AnnotationNotFoundError)
    @app.exception_handler(AnnotationDeletedError)
    def handle_annotation_not_found(
        _request: Request,
        _error: AnnotationNotFoundError | AnnotationDeletedError,
    ) -> JSONResponse:
        return _error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="annotation_not_found",
            message="Annotation was not found",
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
