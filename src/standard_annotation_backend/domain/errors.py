"""Define the application errors that clients can receive.

Every public error is a `SabeError` subclass with a stable `code`, a fixed,
safe `message`, and optional typed details. Each concrete error also derives
from exactly one outcome category below; the API layer maps each category to
one HTTP status, so this module knows nothing about HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

if TYPE_CHECKING:
    from standard_annotation_backend.domain.validation import ValidationIssue


@dataclass(frozen=True, slots=True)
class DuplicatePeers:
    """Identify the active annotations a write would duplicate.

    Attributes:
        peer_ids: Identifiers of the conflicting annotations.
    """

    peer_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class StaleVersion:
    """Describe an annotation whose current version differs from the expected one.

    Attributes:
        annotation_id: Annotation that changed.
        expected_version: Version the client expected.
        current_version: Version that is current now.
    """

    annotation_id: UUID
    expected_version: int
    current_version: int


@dataclass(frozen=True, slots=True)
class StaleChangeSet:
    """Describe a change set whose target changed after its base version.

    Attributes:
        change_set_id: Change set that is now stale.
        annotation_id: Target annotation.
        expected_version: The change set's base version.
        current_version: The target's current version.
    """

    change_set_id: UUID
    annotation_id: UUID
    expected_version: int
    current_version: int


type ErrorDetails = (
    tuple[ValidationIssue, ...] | DuplicatePeers | StaleVersion | StaleChangeSet
)
"""Typed details an error may attach to its response."""


class SabeError(Exception):
    """Report a failure that clients can receive and handle by its `code`.

    Concrete errors set `code` and `message` as class attributes. Constructors
    may take and store identifying values, then call `super().__init__()`; those
    values never appear in `message`.

    Attributes:
        code: Stable machine-readable identifier.
        message: Fixed explanation that is safe to show any client.
    """

    code: ClassVar[str]
    message: ClassVar[str]

    def __init__(self) -> None:
        super().__init__(self.message)

    def details(self) -> ErrorDetails | None:
        """Return typed details for the response, or `None` when there are none."""
        return None


class BadRequestError(SabeError):
    """The request cannot be interpreted at all."""


class UnauthenticatedError(SabeError):
    """No valid identity or management session accompanies the request."""


class ForbiddenError(SabeError):
    """The identity is known but a role, scope, CSRF, or identity check failed."""


class NotFoundError(SabeError):
    """The resource is missing, deleted, or outside the caller's scope.

    These cases are indistinguishable to clients, so callers cannot learn
    whether an inaccessible resource exists.
    """


class MethodNotAllowedError(SabeError):
    """The resource does not support the request's method."""


class ConflictError(SabeError):
    """The request is valid but conflicts with the current state."""


class PreconditionFailedError(SabeError):
    """The version named in `If-Match` is not current."""


class InvalidInputError(SabeError):
    """The input is well formed but violates a rule.

    Responses always carry located validation issues. By default there is one
    issue at `issue_location()` with this error's code and message; errors that
    already hold a list of issues override `details()`.
    """

    def issue_location(self) -> tuple[str | int, ...]:
        """Return where the problem is, such as `("body", "source_key")`.

        The first element names what was checked: `body`, `query`, `path`, or
        `header` for a part of the HTTP request, or `annotation` for the
        annotation the request would produce.
        """
        raise NotImplementedError

    def details(self) -> ErrorDetails | None:
        """Return one validation issue at `issue_location()`."""
        issue: ValidationIssue = {
            "location": self.issue_location(),
            "message": self.message,
            "type": self.code,
        }
        return (issue,)


class PreconditionRequiredError(SabeError):
    """The request must name the version it expects, and does not."""


class InternalServerError(SabeError):
    """The request failed because of a problem in SABE."""


class UpstreamError(SabeError):
    """An upstream service failed."""


class UnavailableError(SabeError):
    """A dependency needed to complete the request is unavailable."""
