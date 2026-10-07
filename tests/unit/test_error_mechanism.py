"""Tests for converting application errors into HTTP responses and docs."""

import json
from typing import Any
from uuid import UUID

from fastapi import status

from standard_annotation_backend.api.errors import (
    RequestValidationFailedError,
    error_responses,
    sab_error_response,
    status_for,
)
from standard_annotation_backend.api.models import ApiErrorResponse
from standard_annotation_backend.domain.annotations import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
    AnnotationVersionNotFoundError,
    EmptyAnnotationPatchError,
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

ANNOTATION_ID = UUID("00000000-0000-0000-0000-000000000001")
CHANGE_SET_ID = UUID("00000000-0000-0000-0000-000000000002")


class _Missing(NotFoundError):
    code = "thing_not_found"
    message = "Thing was not found"


class _Bad(InvalidInputError):
    code = "bad_thing"
    message = "Thing is bad"

    def issue_location(self) -> tuple[str | int, ...]:
        return ("body", "thing")


class _Duplicate(ConflictError):
    code = "duplicate_thing"
    message = "Thing conflicts"

    def details(self) -> ErrorDetails:
        return DuplicatePeers(peer_ids=(ANNOTATION_ID,))


def _body(error: SabError) -> dict[str, Any]:
    return json.loads(bytes(sab_error_response(error).body))


def test_each_base_maps_to_one_status() -> None:
    """Every outcome category has exactly one HTTP status."""
    expected = {
        BadRequestError: status.HTTP_400_BAD_REQUEST,
        UnauthenticatedError: status.HTTP_401_UNAUTHORIZED,
        ForbiddenError: status.HTTP_403_FORBIDDEN,
        NotFoundError: status.HTTP_404_NOT_FOUND,
        ConflictError: status.HTTP_409_CONFLICT,
        PreconditionFailedError: status.HTTP_412_PRECONDITION_FAILED,
        InvalidInputError: status.HTTP_422_UNPROCESSABLE_CONTENT,
        PreconditionRequiredError: status.HTTP_428_PRECONDITION_REQUIRED,
        UpstreamError: status.HTTP_502_BAD_GATEWAY,
        UnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    for base, code in expected.items():
        assert status_for(base) == code


def test_error_without_details_omits_details() -> None:
    """The envelope carries only code and message when there are no details."""
    response = sab_error_response(_Missing())

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert _body(_Missing()) == {
        "error": {"code": "thing_not_found", "message": "Thing was not found"}
    }


def test_invalid_input_reports_one_located_issue_by_default() -> None:
    """A rule violation names where in the request the problem is."""
    assert _body(_Bad())["error"] == {
        "code": "bad_thing",
        "message": "Thing is bad",
        "details": [
            {
                "location": ["body", "thing"],
                "message": "Thing is bad",
                "type": "bad_thing",
            }
        ],
    }


def test_details_values_serialize_to_existing_shapes() -> None:
    """Domain details become the existing public detail models."""
    assert _body(_Duplicate())["error"]["details"] == {"peer_ids": [str(ANNOTATION_ID)]}

    class _Stale(PreconditionFailedError):
        code = "stale"
        message = "Stale"

        def details(self) -> ErrorDetails:
            return StaleVersion(
                annotation_id=ANNOTATION_ID, expected_version=1, current_version=2
            )

    assert _body(_Stale())["error"]["details"] == {
        "annotation_id": str(ANNOTATION_ID),
        "expected_version": 1,
        "current_version": 2,
    }

    class _StaleChangeSet(ConflictError):
        code = "stale_cs"
        message = "Stale change set"

        def details(self) -> ErrorDetails:
            return StaleChangeSet(
                change_set_id=CHANGE_SET_ID,
                annotation_id=ANNOTATION_ID,
                expected_version=1,
                current_version=3,
            )

    assert _body(_StaleChangeSet())["error"]["details"] == {
        "change_set_id": str(CHANGE_SET_ID),
        "annotation_id": str(ANNOTATION_ID),
        "expected_version": 1,
        "current_version": 3,
    }


def test_error_responses_group_codes_by_status() -> None:
    """Route docs list every code each status can carry."""
    responses = error_responses(_Missing, _Bad, _Duplicate)

    assert set(responses) == {404, 409, 422}
    assert responses[404]["model"] is ApiErrorResponse
    assert "`thing_not_found`" in responses[404]["description"]
    assert "`bad_thing`" in responses[422]["description"]


def test_deleted_and_missing_annotations_are_indistinguishable() -> None:
    """A deleted annotation returns exactly what a missing one returns."""
    assert _body(AnnotationDeletedError(ANNOTATION_ID)) == _body(
        AnnotationNotFoundError(ANNOTATION_ID)
    )
    assert sab_error_response(AnnotationDeletedError(ANNOTATION_ID)).status_code == 404


def test_missing_version_has_its_own_code() -> None:
    """A missing version of an existing annotation is reported distinctly."""
    assert _body(AnnotationVersionNotFoundError(ANNOTATION_ID, 3))["error"] == {
        "code": "version_not_found",
        "message": "Annotation version was not found",
    }


def test_empty_patch_names_the_request_body() -> None:
    """An empty patch is invalid input located at the request body."""
    assert _body(EmptyAnnotationPatchError())["error"]["details"] == [
        {
            "location": ["body"],
            "message": "Annotation patch must include at least one field",
            "type": "empty_annotation_patch",
        }
    ]


def test_request_validation_failure_reports_each_issue() -> None:
    """A request FastAPI cannot validate reports every located issue."""
    issue: ValidationIssue = {
        "location": ("body", "annotation", "relation"),
        "message": "Field required",
        "type": "missing",
    }

    assert _body(RequestValidationFailedError((issue,)))["error"] == {
        "code": "request_validation_error",
        "message": "Request validation failed",
        "details": [
            {
                "location": ["body", "annotation", "relation"],
                "message": "Field required",
                "type": "missing",
            }
        ],
    }
