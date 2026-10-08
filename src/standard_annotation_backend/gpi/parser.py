"""Parse GPI files into entity catalogs with the schema package's GPI reader."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Literal

from go_standard_annotation_schema.io import GpiReader, HeaderError, RowIssue
from pydantic import ValidationError

from standard_annotation_backend.domain.entities import (
    EntityCatalog,
    EntityRecord,
)
from standard_annotation_backend.domain.refresh import SourceProvenance
from standard_annotation_backend.domain.validation import field_path
from standard_annotation_backend.exchange_files import (
    NUL_REASON,
    NulHeaderLineError,
    header_metadata,
    nul_data_lines,
)

GPI_SOURCE_FORMAT = "gpi-2.0"
MAX_REPORTED_ROW_ISSUES = 100
MAX_REPORTED_TEXT_LENGTH = 200


@dataclass(frozen=True, slots=True)
class GpiFieldIssue:
    """Describe why one field of a GPI row failed schema validation.

    Attributes:
        field: Dotted path of the field, such as `db_object_symbol`.
        message: The validation message, cut to 200 characters.
        value: The rejected value as text, cut to 200 characters, or `None` when
            the problem is not tied to a single value.
    """

    field: str
    message: str
    value: str | None


@dataclass(frozen=True, slots=True)
class GpiRowIssue:
    """Describe one GPI data row that could not be read.

    Attributes:
        line_number: One-based line number of the row in the source file.
        category: `syntax`, `field-count`, or `validation`, as reported by the
            schema package's reader.
        message: The reason for a `syntax` or `field-count` problem, cut to 200
            characters. `None` for `validation`, whose reasons are in `fields`.
        fields: One entry per invalid field for a `validation` problem.
    """

    line_number: int
    category: str
    message: str | None
    fields: tuple[GpiFieldIssue, ...]

    def to_details(self) -> dict[str, object]:
        """Return the issue as a JSON-compatible dict for job failure details."""
        return {
            "line_number": self.line_number,
            "category": self.category,
            "message": self.message,
            "fields": [
                {"field": field.field, "message": field.message, "value": field.value}
                for field in self.fields
            ],
        }


class GpiParseError(ValueError):
    """Report a GPI parsing failure and describe what was wrong.

    Attributes:
        code: `header` when the header is invalid, or `row_validation` when one
            or more data rows are invalid.
        message: For `header`, a description of the header problem.
        issues: For `row_validation`, every invalid row in line order.
    """

    def __init__(
        self,
        code: Literal["header", "row_validation"],
        *,
        message: str | None = None,
        issues: tuple[GpiRowIssue, ...] = (),
    ) -> None:
        """Store the failure code and its description.

        Args:
            code: `header` or `row_validation`.
            message: Description of a header problem.
            issues: Every invalid data row.
        """
        self.code = code
        self.message = message
        self.issues = issues
        super().__init__(f"GPI parsing failed: {self.code}")

    @property
    def details(self) -> dict[str, object]:
        """Return the failure description recorded on a failed refresh job.

        A header failure returns `{"message": ...}`. A row failure returns the
        total number of invalid rows and the first 100 of them.
        """
        if self.code == "header":
            return {"message": self.message or "invalid GPI header"}
        return {
            "issue_count": len(self.issues),
            "issues": [
                issue.to_details() for issue in self.issues[:MAX_REPORTED_ROW_ISSUES]
            ],
        }


def parse_gpi(text: str, source: SourceProvenance) -> EntityCatalog:
    """Parse a complete GPI 2.0 document into an entity catalog.

    The schema package's reader validates the header, including its 2.0 version,
    and every data row. Each record keeps its one-based line number in the source
    text, counting header, comment, and blank lines. The GPI header is kept as the
    catalog's source metadata.

    A catalog is returned only when every row is valid. Otherwise, all invalid
    rows are collected first, so a single error reports every problem in the
    document.

    Args:
        text: Decoded GPI text.
        source: Provenance of the fetched document, stored with the catalog.

    Returns:
        An `EntityCatalog` holding the validated records, the GPI header as
        source metadata, and `source`.

    Raises:
        GpiParseError: If the header or any data row is invalid. Lines containing
            a NUL character, which PostgreSQL cannot store, are invalid. The error
            describes each problem, including the rejected field values.
    """
    try:
        nul_lines = nul_data_lines(text)
    except NulHeaderLineError as error:
        raise GpiParseError("header", message=error.message) from None
    reader_issues: list[RowIssue] = []
    try:
        with GpiReader(
            StringIO(text), errors="skip", on_error=reader_issues.append
        ) as reader:
            source_metadata = header_metadata(reader.metadata)
            records = tuple(
                EntityRecord(line_number=record.line_number, entity=record.item)
                for record in reader.records()
            )
    except HeaderError as error:
        raise GpiParseError("header", message=_truncate(str(error))) from None

    issues = [
        GpiRowIssue(line_number, "syntax", NUL_REASON, ()) for line_number in nul_lines
    ] + [
        _row_issue(issue)
        for issue in reader_issues
        if issue.line_number not in nul_lines
    ]
    if issues:
        raise GpiParseError(
            "row_validation",
            issues=tuple(sorted(issues, key=lambda issue: issue.line_number)),
        )

    return EntityCatalog(
        source=source,
        source_format=GPI_SOURCE_FORMAT,
        source_metadata=source_metadata,
        records=records,
        warnings=(),
    )


def _row_issue(issue: RowIssue) -> GpiRowIssue:
    """Convert the schema reader's description of a rejected row."""
    line_number = issue.line_number or 0
    if isinstance(issue.cause, ValidationError):
        return GpiRowIssue(
            line_number=line_number,
            category=issue.code,
            message=None,
            fields=tuple(
                GpiFieldIssue(
                    field=field_path(error["loc"]),
                    message=_truncate(error["msg"]),
                    value=_reported_value(error.get("input")),
                )
                for error in issue.cause.errors()
            ),
        )
    return GpiRowIssue(
        line_number=line_number,
        category=issue.code,
        message=_truncate(str(issue.cause)),
        fields=(),
    )


def _reported_value(value: object) -> str | None:
    """Return a rejected scalar value as truncated text, or `None` otherwise."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    return _truncate(str(value))


def _truncate(text: str) -> str:
    """Cut text to the length stored in job failure details."""
    return text[:MAX_REPORTED_TEXT_LENGTH]
