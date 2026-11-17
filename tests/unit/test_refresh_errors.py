"""Tests for the failure and outcome types shared by every refresh."""

import pytest

from standard_annotation_backend.domain.annotation_management import (
    AnnotationImportConflictError,
    GroupSabeManagedError,
)
from standard_annotation_backend.domain.entities import EntityCatalogCollisionError
from standard_annotation_backend.domain.jobs import JobFailureCode
from standard_annotation_backend.domain.refresh import (
    RefreshKindName,
    TerminalRefreshError,
    UnknownSourceError,
)
from standard_annotation_backend.refresh.fetchers import SourceError


class _FixedCodeError(TerminalRefreshError):
    failure_code = JobFailureCode.CATALOG_COLLISION

    def __init__(self) -> None:
        super().__init__("identifiers collide")


def test_terminal_error_records_a_code_and_details_given_by_the_raiser() -> None:
    """A translated parser failure carries its code and details to the runner."""
    error = TerminalRefreshError(
        failure_code=JobFailureCode.HEADER, failure_details={"message": "bad"}
    )

    assert error.failure_code is JobFailureCode.HEADER
    assert error.failure_details == {"message": "bad"}
    assert str(error) == "Refresh failed: header"


def test_terminal_error_subclass_declares_its_code_and_keeps_its_message() -> None:
    """A domain error with a fixed code needs no translation and keeps its text."""
    error = _FixedCodeError()

    assert error.failure_code is JobFailureCode.CATALOG_COLLISION
    assert error.failure_details is None
    assert str(error) == "identifiers collide"


def test_terminal_error_without_any_code_is_a_programming_error() -> None:
    """Raising the base class without a code fails loudly instead of guessing."""
    with pytest.raises(TypeError):
        TerminalRefreshError()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (SourceError(JobFailureCode.TIMEOUT), JobFailureCode.TIMEOUT),
        (
            UnknownSourceError(RefreshKindName.ENTITY, "mgi"),
            JobFailureCode.UNKNOWN_SOURCE,
        ),
        (
            EntityCatalogCollisionError(("MGI:1",)),
            JobFailureCode.CATALOG_COLLISION,
        ),
        (GroupSabeManagedError("MGI"), JobFailureCode.GROUP_SABE_MANAGED),
        (AnnotationImportConflictError(), JobFailureCode.INVALID_PARAMETERS),
    ],
)
def test_refresh_only_errors_declare_their_failure_code(
    error: TerminalRefreshError, code: JobFailureCode
) -> None:
    """Errors that only mean "this refresh failed" need no per-kind mapping."""
    assert isinstance(error, TerminalRefreshError)
    assert error.failure_code is code
    assert error.failure_details is None


def test_source_error_keeps_its_code_attribute_and_message() -> None:
    """Retrieval errors still expose `code` and a message without the URL."""
    error = SourceError(JobFailureCode.HTTP_STATUS)

    assert error.code is JobFailureCode.HTTP_STATUS
    assert str(error) == "Source retrieval failed: http_status"


def test_source_error_rejects_a_code_that_is_not_a_retrieval_failure() -> None:
    """Only retrieval and decoding codes describe a source failure."""
    with pytest.raises(ValueError, match="retrieval"):
        SourceError(JobFailureCode.HEADER)
