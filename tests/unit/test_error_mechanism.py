"""Tests for converting application errors into HTTP responses and docs."""

import importlib
import inspect
import json
import pkgutil
import re
from typing import Any
from uuid import UUID

from fastapi import status

import standard_annotation_backend
from standard_annotation_backend.api.errors import (
    BEARER_ERRORS,
    STATUS_BY_BASE,
    error_responses,
    sab_error_response,
    status_for,
)
from standard_annotation_backend.api.models import ApiErrorResponse
from standard_annotation_backend.domain.annotations import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
)
from standard_annotation_backend.domain.auth import (
    AuthenticationRequiredError,
)
from standard_annotation_backend.domain.comments import InvalidCommentError
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
from standard_annotation_backend.domain.tokens import (
    ManagementSessionRequiredError,
)

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
        MethodNotAllowedError: status.HTTP_405_METHOD_NOT_ALLOWED,
        ConflictError: status.HTTP_409_CONFLICT,
        PreconditionFailedError: status.HTTP_412_PRECONDITION_FAILED,
        InvalidInputError: status.HTTP_422_UNPROCESSABLE_CONTENT,
        PreconditionRequiredError: status.HTTP_428_PRECONDITION_REQUIRED,
        InternalServerError: status.HTTP_500_INTERNAL_SERVER_ERROR,
        UpstreamError: status.HTTP_502_BAD_GATEWAY,
        UnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    for base, code in expected.items():
        assert status_for(base) == code
    assert {base for base, _ in STATUS_BY_BASE} == set(expected)


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
    assert (
        sab_error_response(AnnotationDeletedError(ANNOTATION_ID)).status_code
        == status.HTTP_404_NOT_FOUND
    )


def test_blank_comment_names_the_comment_body_field() -> None:
    """A comment with no visible text is invalid input located at its body field."""
    assert _body(InvalidCommentError())["error"] == {
        "code": "invalid_comment",
        "message": "Comment body must contain non-whitespace text",
        "details": [
            {
                "location": ["body", "body"],
                "message": "Comment body must contain non-whitespace text",
                "type": "invalid_comment",
            }
        ],
    }


def test_only_missing_bearer_authentication_sends_a_bearer_challenge() -> None:
    """Bearer failures challenge with `WWW-Authenticate`; management sessions do not."""
    bearer = sab_error_response(AuthenticationRequiredError())
    session = sab_error_response(ManagementSessionRequiredError())

    assert bearer.status_code == session.status_code == status.HTTP_401_UNAUTHORIZED
    assert bearer.headers["WWW-Authenticate"] == "Bearer"
    assert "WWW-Authenticate" not in session.headers
    assert _body(AuthenticationRequiredError()) == {
        "error": {
            "code": "authentication_required",
            "message": "Authentication required",
        }
    }
    assert _body(ManagementSessionRequiredError()) == {
        "error": {
            "code": "management_session_required",
            "message": "A current token-management session is required",
        }
    }


def test_response_headers_merge_with_the_bearer_challenge() -> None:
    """Extra response headers are added without dropping the bearer challenge."""
    response = sab_error_response(
        AuthenticationRequiredError(), headers={"Allow": "GET"}
    )

    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["Allow"] == "GET"


def test_bearer_error_docs_list_each_code_and_the_challenge_header() -> None:
    """Bearer-protected routes document 401, 403, 500, and 503 with their codes."""
    responses = error_responses(*BEARER_ERRORS)

    assert set(responses) == {401, 403, 500, 503}
    assert "`internal_error`" in responses[500]["description"]
    assert "`authentication_required`" in responses[401]["description"]
    assert responses[401]["headers"]["WWW-Authenticate"]["schema"] == {
        "type": "string",
        "const": "Bearer",
    }
    assert "`permission_denied`" in responses[403]["description"]
    assert "`credential_storage_unavailable`" in responses[503]["description"]


_BASES = tuple(base for base, _ in STATUS_BY_BASE)


def _assigned(error_type: type[SabError], name: str) -> bool:
    """Return whether a class or one of its ancestors assigns `name`.

    `SabError` and the outcome categories only annotate `code` and `message`,
    so an assignment can only come from a class below them.
    """
    return any(name in klass.__dict__ for klass in error_type.__mro__)


def _needs_definition(error_type: type[SabError]) -> bool:
    """Return whether a class can be raised and so must be fully defined.

    Grouping classes that assign no `code` and have subclasses are exempt.
    """
    return not error_type.__subclasses__() or _assigned(error_type, "code")


def _definition_problems(error_type: type[SabError]) -> list[str]:
    """Describe what keeps an error class from producing a safe response."""
    problems: list[str] = []
    for name in ("code", "message"):
        if not _assigned(error_type, name) or not getattr(error_type, name):
            problems.append(f"missing {name}")
    if _assigned(error_type, "message") and re.search(
        r"\{.*\}|%[sdr]", error_type.message
    ):
        problems.append("message has a format placeholder")
    if sum(issubclass(error_type, base) for base in _BASES) != 1:
        problems.append("not exactly one outcome category")
    if (
        issubclass(error_type, InvalidInputError)
        and error_type.issue_location is InvalidInputError.issue_location
        and error_type.details is InvalidInputError.details
    ):
        problems.append("no issue location or details")
    return problems


def _application_errors() -> list[type[SabError]]:
    """Return every error class SAB defines that must be fully defined."""
    for module in pkgutil.walk_packages(
        standard_annotation_backend.__path__, "standard_annotation_backend."
    ):
        importlib.import_module(module.name)
    found: list[type[SabError]] = []
    pending = list(SabError.__subclasses__())
    while pending:
        error_type = pending.pop()
        pending.extend(error_type.__subclasses__())
        if (
            error_type not in _BASES
            and error_type.__module__.startswith("standard_annotation_backend.")
            and _needs_definition(error_type)
            and error_type not in found
        ):
            found.append(error_type)
    return found


def test_every_public_error_is_complete_and_unambiguous() -> None:
    """Each error has a code, a fixed message, one outcome, and located issues."""
    errors = _application_errors()
    assert errors
    problems = {
        error_type.__name__: found
        for error_type in errors
        if (found := _definition_problems(error_type))
    }
    assert problems == {}
    codes: dict[str, type[SabError]] = {}
    for error_type in errors:
        owner = codes.setdefault(error_type.code, error_type)
        assert (
            owner is error_type
            or issubclass(error_type, owner)
            or issubclass(owner, error_type)
        ), f"{error_type.__name__} reuses {owner.__name__}'s code"


_ISSUE_ROOTS = {"body", "query", "path", "header", "annotation"}
"""Allowed first elements of an issue location.

`body`, `query`, `path`, and `header` name the part of the HTTP request that was
checked; `annotation` names the annotation the request would produce.
"""


def _representative(error_type: type[InvalidInputError]) -> InvalidInputError:
    """Construct an error, passing a placeholder for each required argument."""
    parameters = [
        parameter
        for parameter in inspect.signature(error_type).parameters.values()
        if parameter.default is inspect.Parameter.empty
    ]
    return error_type(*("placeholder" for _ in parameters))


def test_every_default_issue_location_starts_with_an_allowed_root() -> None:
    """Each error's own issue location names what was checked first."""
    located = [
        error_type
        for error_type in _application_errors()
        if issubclass(error_type, InvalidInputError)
        and error_type.issue_location is not InvalidInputError.issue_location
    ]
    assert len(located) >= 10
    roots = {
        error_type.__name__: _representative(error_type).issue_location()[0]
        for error_type in located
    }
    assert {
        name: root for name, root in roots.items() if root not in _ISSUE_ROOTS
    } == {}
