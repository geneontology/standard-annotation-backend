"""Parse GPI files into entity catalogs with the schema package's GPI reader."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from io import StringIO

from go_standard_annotation_schema.io import (
    GpiReader,
    HeaderError,
    RowError,
)

from standard_annotation_backend.domain.entities import (
    EntityCatalog,
    EntityRecord,
    EntitySource,
)
from standard_annotation_backend.entity_sources.http import EntitySourceDocument

GPI_SOURCE_FORMAT = "gpi-2.0"


@dataclass(frozen=True, slots=True)
class GpiMetadata:
    """Contain validated GPI header metadata in source order."""

    version: str
    generated_by: str
    date_generated: date | datetime
    entries: tuple[tuple[str, str], ...]

    def to_source_metadata(self) -> dict[str, object]:
        """Return the header as a JSON-compatible dict stored with the catalog."""
        return {
            "version": self.version,
            "generated_by": self.generated_by,
            "date_generated": self.date_generated.isoformat(),
            "entries": [list(entry) for entry in self.entries],
        }


class GpiParseError(ValueError):
    """Report a GPI parsing failure as the code `header` or `row_validation`."""

    def __init__(self, code: str) -> None:
        """Store the failure code without any source text.

        Args:
            code: `header` or `row_validation`. Any other value is stored as
                `row_validation`.
        """
        self.code = code if code in {"header", "row_validation"} else "row_validation"
        super().__init__(f"GPI parsing failed: {self.code}")


def parse_gpi(document: EntitySourceDocument) -> EntityCatalog:
    """Parse a complete GPI 2.0 document into an entity catalog.

    The schema package's reader validates the header, including its 2.0 version,
    and every data row. Each record keeps its one-based line number in the source
    text, counting header, comment, and blank lines. The GPI header is kept as the
    catalog's source metadata.

    Args:
        document: Retrieved source text and the details that identify it.

    Returns:
        The validated entity records, the GPI header, and the source details.

    Raises:
        GpiParseError: If the header or any data row is invalid, or any line
            contains a NUL character, which PostgreSQL cannot store. The error
            does not include source text.
    """
    for line in StringIO(document.text):
        if "\x00" in line:
            raise GpiParseError("header" if line.startswith("!") else "row_validation")
    parse_error_code: str | None = None
    try:
        with GpiReader(StringIO(document.text), errors="strict") as reader:
            reader_metadata = reader.metadata
            metadata = GpiMetadata(
                version=reader_metadata.version,
                generated_by=reader_metadata.generated_by,
                date_generated=reader_metadata.date_generated,
                entries=tuple(
                    (entry.key, entry.value) for entry in reader_metadata.entries
                ),
            )
            records = tuple(
                EntityRecord(line_number=record.line_number, entity=record.item)
                for record in reader.records()
            )
    except HeaderError:
        parse_error_code = "header"
    except RowError:
        parse_error_code = "row_validation"

    if parse_error_code is not None:
        raise GpiParseError(parse_error_code)

    return EntityCatalog(
        source=EntitySource(
            source_url=document.source_url,
            source_checksum=document.source_checksum,
            fetched_at=document.fetched_at,
        ),
        source_format=GPI_SOURCE_FORMAT,
        source_metadata=metadata.to_source_metadata(),
        records=records,
        warnings=(),
    )
