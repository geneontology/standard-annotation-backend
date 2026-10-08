"""Share checks between the GPI and GPAD readers.

GPI and GPAD are GO exchange files: tab-separated text whose header lines start
with `!`. Both are read with the `go_standard_annotation_schema` readers, and SAB
applies the same extra checks to each before storing their contents.
"""

from io import StringIO

from go_standard_annotation_schema.io import FileMetadata

NUL_REASON = "contains a NUL character"
"""Reason reported for a data line that contains a NUL character."""


class NulHeaderLineError(ValueError):
    """Report a header line that contains a NUL character.

    Each reader translates this into its own header error.

    Attributes:
        line_number: One-based line number of the header line.
        message: Description of the problem naming the line.
    """

    def __init__(self, line_number: int) -> None:
        """Store the offending line number.

        Args:
            line_number: One-based line number of the header line.
        """
        self.line_number = line_number
        self.message = f"line {line_number} {NUL_REASON}"
        super().__init__(self.message)


def nul_data_lines(text: str) -> frozenset[int]:
    """Find the data lines that contain a NUL character.

    PostgreSQL text columns cannot store the NUL character. A header line that
    contains one makes the whole document invalid; a data line that contains one
    is reported as an invalid row by the caller.

    Args:
        text: Decoded GPI or GPAD text.

    Returns:
        One-based line numbers of data lines that contain a NUL character.

    Raises:
        NulHeaderLineError: If a header line contains a NUL character. The first
            such line in the document is reported.
    """
    nul_lines: set[int] = set()
    for line_number, line in enumerate(StringIO(text), start=1):
        if "\x00" not in line:
            continue
        if line.startswith("!"):
            raise NulHeaderLineError(line_number)
        nul_lines.add(line_number)
    return frozenset(nul_lines)


def header_metadata(metadata: FileMetadata) -> dict[str, object]:
    """Return validated header values as a JSON-compatible dict.

    Args:
        metadata: Header read by a `go_standard_annotation_schema` reader.

    Returns:
        The `version`, `generated_by`, and ISO-formatted `date_generated` values,
        and the remaining header `entries` as key-value pairs in source order.
    """
    return {
        "version": metadata.version,
        "generated_by": metadata.generated_by,
        "date_generated": metadata.date_generated.isoformat(),
        "entries": [[entry.key, entry.value] for entry in metadata.entries],
    }
