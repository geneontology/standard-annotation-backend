"""Read GO exchange files: GPI and GPAD.

GPI and GPAD are GO exchange files: tab-separated text whose header lines start
with `!`. Both are read with the `go_standard_annotation_schema` readers.
`ExchangeDocument` wraps either reader with the checks SABE applies before
storing a file's contents, and turns every row the reader skips into a
`RowRejection`, so a refresh can load the valid rows and report the rest.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from io import StringIO
from types import TracebackType
from typing import Literal, Protocol, TextIO

from go_standard_annotation_schema.io import (
    FileMetadata,
    HeaderError,
    ReaderStats,
    Record,
    RowIssue,
)
from pydantic import BaseModel, ValidationError

from standard_annotation_backend.domain.jobs import JobFailureCode
from standard_annotation_backend.domain.refresh import (
    MAX_REJECTION_REASON_LENGTH,
    RowRejection,
    TerminalRefreshError,
)
from standard_annotation_backend.domain.validation import field_path

_NUL_REASON = "contains a NUL character"
"""Reason reported for a data line that contains a NUL character."""


def _nul_data_lines(text: str) -> frozenset[int]:
    """Find the data lines that contain a NUL character.

    PostgreSQL text columns cannot store the NUL character, so a data line that
    contains one is rejected and a header line that contains one fails the
    document.

    Raises:
        ExchangeHeaderError: If a header line contains a NUL character. The first
            such line in the document is reported.
    """
    nul_lines: set[int] = set()
    for line_number, line in enumerate(StringIO(text), start=1):
        if "\x00" not in line:
            continue
        if line.startswith("!"):
            raise ExchangeHeaderError(f"line {line_number} {_NUL_REASON}")
        nul_lines.add(line_number)
    return frozenset(nul_lines)


def _header_metadata(metadata: FileMetadata) -> dict[str, object]:
    """Return validated header values as a JSON-compatible dict.

    Args:
        metadata: Header read by a `go_standard_annotation_schema` reader.

    Returns:
        The `version`, `generated_by`, and ISO-formatted `date_generated` values,
        and `entries`: every `key: value` header line, including those three, as
        key-value pairs in source order.
    """
    return {
        "version": metadata.version,
        "generated_by": metadata.generated_by,
        "date_generated": metadata.date_generated.isoformat(),
        "entries": [[entry.key, entry.value] for entry in metadata.entries],
    }


class ExchangeHeaderError(TerminalRefreshError):
    """Report an invalid GPI or GPAD header.

    A header is invalid when it is missing, malformed, not version 2.0, or contains
    a NUL character. No row of a file can be trusted when its header cannot, so the
    refresh fails with `header` and stores `message` in its failure details.

    Attributes:
        message: Description of the problem, at most 1,000 characters.
    """

    failure_code = JobFailureCode.HEADER

    def __init__(self, message: str) -> None:
        self.message = message[:MAX_REJECTION_REASON_LENGTH]
        super().__init__(
            "exchange file header is invalid",
            failure_details={"message": self.message},
        )


class _SchemaReader[ModelT: BaseModel](Protocol):
    """The parts of a `go_standard_annotation_schema` reader that SABE uses."""

    @property
    def metadata(self) -> FileMetadata: ...

    @property
    def stats(self) -> ReaderStats: ...

    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def records(self) -> Iterator[Record[ModelT]]: ...


class _ReaderFactory[ModelT: BaseModel](Protocol):
    """Construct a schema reader the way `ExchangeDocument` does.

    `ExchangeDocument` passes the decoded text as a stream, skips invalid rows, and
    collects each skipped row's issue. `GpiReader` and `GpadReader` match.
    """

    def __call__(
        self,
        stream: TextIO,
        /,
        *,
        errors: Literal["skip"],
        on_error: Callable[[RowIssue], None],
    ) -> _SchemaReader[ModelT]: ...


class ExchangeDocument[ModelT: BaseModel]:
    """Read one decoded GPI or GPAD document as records and rejected rows.

    Use it as a context manager. Entering reads and validates the header, so a
    header problem raises before any row is read. `items` then streams the rows
    without holding every record in memory. The GPAD reader can yield several
    records for one row, one for each combination of the pipe-separated
    alternatives in its with/from and extension columns, all with the row's line
    number.

    PostgreSQL cannot store the NUL character, so a data line containing one is
    rejected with code `syntax`, and a header line containing one fails the
    document.

    Args:
        reader_type: `GpiReader` or `GpadReader`.
        text: Decoded document text.

    Attributes:
        metadata: Header values, set on entering: `version`, `generated_by`,
            `date_generated`, and `entries`, every `key: value` header line as a
            key-value pair.
    """

    def __init__(self, reader_type: _ReaderFactory[ModelT], text: str) -> None:
        self._reader_type = reader_type
        self._text = text
        self._issues: deque[RowIssue] = deque()
        self._reader: _SchemaReader[ModelT] | None = None
        self._nul_lines: frozenset[int] = frozenset()
        self.metadata: dict[str, object] = {}

    def __enter__(self) -> ExchangeDocument[ModelT]:
        """Validate the header and prepare to read rows.

        Raises:
            ExchangeHeaderError: If the header is invalid or contains a NUL
                character.
        """
        self._nul_lines = _nul_data_lines(self._text)
        reader = self._reader_type(
            StringIO(self._text), errors="skip", on_error=self._issues.append
        )
        try:
            reader.__enter__()
        except HeaderError as error:
            raise ExchangeHeaderError(str(error)) from None
        self._reader = reader
        self.metadata = _header_metadata(reader.metadata)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the underlying reader."""
        if self._reader is not None:
            self._reader.__exit__(exc_type, exc_value, traceback)

    @property
    def data_rows(self) -> int:
        """Return how many data rows have been read so far, including rejected ones."""
        return 0 if self._reader is None else self._reader.stats.data_rows

    def items(self) -> Iterator[Record[ModelT] | RowRejection]:
        """Yield records and rejections in line order.

        Raises:
            RuntimeError: If iteration starts before the document is entered.
        """
        if self._reader is None:
            raise RuntimeError("ExchangeDocument must be entered before reading items")
        reported_nul: set[int] = set()
        for record in self._reader.records():
            # The reader reports a skipped row before it yields the next valid
            # one, so draining here keeps everything in line order.
            yield from self._drain_issues(reported_nul)
            if record.line_number in self._nul_lines:
                yield from self._nul_rejection(record.line_number, reported_nul)
                continue
            yield record
        yield from self._drain_issues(reported_nul)

    def _drain_issues(self, reported_nul: set[int]) -> Iterator[RowRejection]:
        while self._issues:
            issue = self._issues.popleft()
            line_number = issue.line_number or 0
            if line_number in self._nul_lines:
                yield from self._nul_rejection(line_number, reported_nul)
            else:
                yield _rejection(issue)

    @staticmethod
    def _nul_rejection(
        line_number: int, reported_nul: set[int]
    ) -> Iterator[RowRejection]:
        if line_number not in reported_nul:
            reported_nul.add(line_number)
            yield RowRejection(line_number, "syntax", _NUL_REASON)


def _rejection(issue: RowIssue) -> RowRejection:
    """Convert a row that the schema reader skipped into a rejection.

    A `validation` reason lists each invalid field as `field: message`, separated
    by semicolons. The rejected value is not added, although a message may quote
    it. Every reason is cut to 1,000 characters.
    """
    if isinstance(issue.cause, ValidationError):
        reason = "; ".join(
            f"{field_path(error['loc'])}: {error['msg']}"
            for error in issue.cause.errors()
        )
    else:
        reason = str(issue.cause)
    return RowRejection(
        issue.line_number or 0, issue.code, reason[:MAX_REJECTION_REASON_LENGTH]
    )
