"""Read GPAD 2.0 documents with the `go_standard_annotation_schema` GPAD reader.

An ordinary annotation refresh publishes every valid annotation and reports the
rest, so rows that cannot be read become rejections instead of failing the whole
document. Only an invalid header fails it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from io import StringIO
from types import TracebackType

from go_standard_annotation_schema.io import GpadReader, HeaderError, RowIssue
from pydantic import ValidationError

from standard_annotation_backend.domain.annotation_management import (
    MAX_REJECTION_REASON_LENGTH,
    AnnotationRejection,
)
from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.validation import field_path
from standard_annotation_backend.exchange_files import (
    NUL_REASON,
    NulHeaderLineError,
    header_metadata,
    nul_data_lines,
)


class GpadHeaderError(ValueError):
    """Report a GPAD header that is missing, malformed, or not version 2.0.

    Attributes:
        message: Description of the problem, at most 200 characters.
    """

    def __init__(self, message: str) -> None:
        self.message = message[:MAX_REJECTION_REASON_LENGTH]
        super().__init__("GPAD header is invalid")


@dataclass(frozen=True, slots=True)
class ParsedAnnotation:
    """Pair a validated annotation with the GPAD line it came from.

    Attributes:
        line_number: One-based line number in the source text.
        annotation: The validated Standard Annotation.
    """

    line_number: int
    annotation: Annotation


class GpadDocument:
    """Read one decoded GPAD document as annotations and rejected records.

    Use it as a context manager. Entering reads and validates the header, so
    header problems raise before any row is read. `items` then streams the
    document without holding every annotation in memory. A row whose with/from
    or extension column has pipe-separated alternatives yields one annotation
    per alternative, all with the row's line number.

    PostgreSQL cannot store the NUL character, so a data line containing one is
    rejected with code `syntax`, and a header line containing one fails the
    document.

    Args:
        text: Decoded GPAD text.

    Attributes:
        metadata: Header values, set on entering: `version`, `generated_by`,
            `date_generated`, and the remaining header `entries` as key-value
            pairs.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._issues: deque[RowIssue] = deque()
        self._reader: GpadReader | None = None
        self._nul_lines: frozenset[int] = frozenset()
        self.metadata: dict[str, object] = {}

    def __enter__(self) -> GpadDocument:
        """Validate the header and prepare to read rows.

        Raises:
            GpadHeaderError: If the header is invalid or contains a NUL character.
        """
        try:
            self._nul_lines = nul_data_lines(self._text)
        except NulHeaderLineError as error:
            raise GpadHeaderError(error.message) from None
        reader = GpadReader(
            StringIO(self._text), errors="skip", on_error=self._issues.append
        )
        try:
            reader.__enter__()
        except HeaderError as error:
            raise GpadHeaderError(str(error)) from None
        self._reader = reader
        self.metadata = header_metadata(reader.metadata)
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

    def items(self) -> Iterator[ParsedAnnotation | AnnotationRejection]:
        """Yield annotations and rejections in line order.

        Raises:
            RuntimeError: If called outside the context manager.
        """
        if self._reader is None:
            raise RuntimeError("GpadDocument must be entered before reading items")
        reported_nul: set[int] = set()
        for record in self._reader.records():
            # The reader reports a skipped row before it yields the next valid
            # one, so draining here keeps everything in line order.
            yield from self._drain_issues(reported_nul)
            if record.line_number in self._nul_lines:
                yield from self._nul_rejection(record.line_number, reported_nul)
                continue
            yield ParsedAnnotation(record.line_number, record.item)
        yield from self._drain_issues(reported_nul)

    def _drain_issues(self, reported_nul: set[int]) -> Iterator[AnnotationRejection]:
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
    ) -> Iterator[AnnotationRejection]:
        if line_number not in reported_nul:
            reported_nul.add(line_number)
            yield AnnotationRejection(line_number, "syntax", NUL_REASON)


def _rejection(issue: RowIssue) -> AnnotationRejection:
    """Convert a row that the schema reader skipped into a rejection.

    Validation reasons name each invalid field and its message, without the
    rejected value.
    """
    if isinstance(issue.cause, ValidationError):
        reason = "; ".join(
            f"{field_path(error['loc'])}: {error['msg']}"
            for error in issue.cause.errors()
        )
    else:
        reason = str(issue.cause)
    return AnnotationRejection(
        issue.line_number or 0, issue.code, reason[:MAX_REJECTION_REASON_LENGTH]
    )
