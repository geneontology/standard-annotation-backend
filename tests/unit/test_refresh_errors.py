"""Tests for the failure and outcome types shared by every refresh."""

import pytest

from standard_annotation_backend.domain.annotation_management import (
    AnnotationImportConflictError,
    CutoverRejectedError,
    GroupSabeManagedError,
)
from standard_annotation_backend.domain.jobs import JobFailureCode
from standard_annotation_backend.domain.refresh import (
    NoValidRowsError,
    RefreshKindName,
    RejectionReport,
    RowRejection,
    TerminalRefreshError,
    UnknownSourceError,
)
from standard_annotation_backend.refresh.fetchers import SourceError


class _FixedCodeError(TerminalRefreshError):
    failure_code = JobFailureCode.INVALID_DOCUMENT

    def __init__(self) -> None:
        super().__init__("document is invalid")


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

    assert error.failure_code is JobFailureCode.INVALID_DOCUMENT
    assert error.failure_details is None
    assert str(error) == "document is invalid"


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


_REPORT = RejectionReport(
    issue_count=1,
    by_code={"syntax": 1},
    issues=(RowRejection(4, "syntax", "bad line"),),
)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (NoValidRowsError(_REPORT), JobFailureCode.NO_VALID_ROWS),
        (CutoverRejectedError(_REPORT), JobFailureCode.CUTOVER_REJECTED),
    ],
    ids=["no-valid-rows", "cutover-rejected"],
)
def test_rejected_row_failures_store_the_report_as_details(
    error: NoValidRowsError | CutoverRejectedError, code: JobFailureCode
) -> None:
    """A file with no valid row, or a cutover with a rejected row, fails its job.

    The job records the failure's code and the rejection report as its details.
    """
    assert error.failure_code is code
    assert error.failure_details == {
        "issue_count": 1,
        "by_code": {"syntax": 1},
        "issues": [{"line_number": 4, "code": "syntax", "reason": "bad line"}],
    }


def test_source_error_keeps_its_code_attribute_and_message() -> None:
    """Retrieval errors still expose `code` and a message without the URL."""
    error = SourceError(JobFailureCode.HTTP_STATUS)

    assert error.code is JobFailureCode.HTTP_STATUS
    assert str(error) == "Source retrieval failed: http_status"


def test_source_error_rejects_a_code_that_is_not_a_retrieval_failure() -> None:
    """Only retrieval and decoding codes describe a source failure."""
    with pytest.raises(ValueError, match="retrieval"):
        SourceError(JobFailureCode.HEADER)
