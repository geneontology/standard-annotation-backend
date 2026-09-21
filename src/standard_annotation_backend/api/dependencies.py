"""Create request-specific objects used by API routes."""

import re
from typing import Annotated

from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from standard_annotation_backend.api.errors import ApiError
from standard_annotation_backend.domain.auth import (
    AuthenticationRequiredError,
    RequestContext,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_service import AnnotationService
from standard_annotation_backend.services.authentication_service import (
    AuthenticationService,
)
from standard_annotation_backend.services.change_set_service import ChangeSetService

_IF_MATCH_PATTERN = re.compile(r'^"([1-9][0-9]*)"$')

# The dependency reads raw headers so it can return 428 for an absent value
# and reject repeated physical lines. Declare the required public input explicitly.
IF_MATCH_OPENAPI = {
    "parameters": [
        {
            "name": "If-Match",
            "in": "header",
            "required": True,
            "description": (
                "Exactly one quoted, positive-integer annotation version. "
                "Missing values return 428; malformed or repeated values return 400."
            ),
            "schema": {
                "type": "string",
                "pattern": _IF_MATCH_PATTERN.pattern,
                "example": '"1"',
            },
        }
    ]
}


def _malformed_precondition() -> ApiError:
    return ApiError(
        status_code=status.HTTP_400_BAD_REQUEST,
        code="malformed_precondition",
        message="If-Match must be one quoted positive integer",
    )


def parse_if_match(if_match: str | None) -> int:
    """Read an annotation version from an `If-Match` header value.

    The API represents versions as quoted positive integers, such as `"3"`.
    Weak entity tags and unquoted values are not accepted.

    Args:
        if_match: Raw header value, or `None` when the header is absent.

    Returns:
        The positive annotation version contained in the header.

    Raises:
        ApiError: If the header is missing or is not one quoted positive integer.
    """
    if if_match is None:
        raise ApiError(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            code="precondition_required",
            message="If-Match is required",
        )

    match = _IF_MATCH_PATTERN.fullmatch(if_match)
    if match is None:
        raise _malformed_precondition()
    try:
        return int(match.group(1))
    except ValueError as error:
        raise _malformed_precondition() from error


def require_expected_version(request: Request) -> int:
    """Require exactly one valid `If-Match` version header.

    Args:
        request: Incoming HTTP request containing the header.

    Returns:
        The annotation version that the client expects to modify.

    Raises:
        ApiError: If the header is missing, repeated, or malformed.
    """
    values = request.headers.getlist("If-Match")
    if len(values) > 1:
        raise _malformed_precondition()
    return parse_if_match(values[0] if values else None)


def get_unit_of_work_factory(request: Request) -> UnitOfWorkFactory:
    """Return the callable that opens a database transaction for a request.

    Args:
        request: Incoming request whose application holds the callable.

    Returns:
        A callable that creates the repositories and transaction for an operation.
    """
    factory: UnitOfWorkFactory = request.app.state.unit_of_work_factory
    return factory


_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="Bearer",
    description="An opaque API token created through token management.",
)


def get_authenticated_context(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    unit_of_work_factory: Annotated[
        UnitOfWorkFactory, Depends(get_unit_of_work_factory)
    ],
) -> RequestContext:
    """Authenticate a bearer token and return its current request context.

    Args:
        request: Incoming request used to reject repeated authorization headers.
        credentials: Bearer credential parsed by FastAPI, when supplied.
        unit_of_work_factory: Callable that opens the authentication transaction.

    Returns:
        Authenticated user, token identity, and selected authorization.

    Raises:
        AuthenticationRequiredError: If exactly one bearer credential is not supplied
            or the credential cannot authenticate.
        CredentialPersistenceError: If credential lookup or recording use fails.
    """
    if credentials is None or len(request.headers.getlist("Authorization")) != 1:
        raise AuthenticationRequiredError
    return AuthenticationService(unit_of_work_factory).authenticate(
        credentials.credentials
    )


def get_annotation_service(
    unit_of_work_factory: Annotated[
        UnitOfWorkFactory,
        Depends(get_unit_of_work_factory),
    ],
) -> AnnotationService:
    """Create the annotation operations used by one request.

    Args:
        unit_of_work_factory: Callable that opens a database transaction.

    Returns:
        A service configured to run annotation operations.
    """
    return AnnotationService(unit_of_work_factory)


def get_change_set_service(
    unit_of_work_factory: Annotated[
        UnitOfWorkFactory,
        Depends(get_unit_of_work_factory),
    ],
) -> ChangeSetService:
    """Create proposal and review operations for one request.

    Args:
        unit_of_work_factory: Callable that opens a database transaction.

    Returns:
        Service configured to run change-set operations.
    """
    return ChangeSetService(unit_of_work_factory)
