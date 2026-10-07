"""Verify the checks shared by the GPI and GPAD readers."""

from datetime import date

import pytest
from go_standard_annotation_schema.io import FileMetadata, MetadataEntry

from standard_annotation_backend.exchange_files import (
    NulHeaderLineError,
    header_metadata,
    nul_data_lines,
)


def test_nul_data_lines_returns_one_based_lines_containing_nul() -> None:
    """Data lines with a NUL character are reported by one-based line number."""
    text = "!gpi-version: 2.0\nfirst\nsec\x00ond\nthird\nfo\x00urth"

    assert nul_data_lines(text) == frozenset({3, 5})


def test_nul_data_lines_is_empty_for_text_without_nul() -> None:
    """Text without a NUL character has no lines to reject."""
    assert nul_data_lines("!gpad-version: 2.0\nrow\n") == frozenset()


def test_nul_in_header_line_raises_with_its_line_number() -> None:
    """A NUL character in a header line rejects the document at that line."""
    text = "!gpi-version: 2.0\nda\x00ta\n!custom: va\x00lue\n"

    with pytest.raises(NulHeaderLineError) as caught:
        nul_data_lines(text)

    assert caught.value.line_number == 3
    assert caught.value.message == "line 3 contains a NUL character"


def test_header_metadata_is_json_compatible_and_keeps_entry_order() -> None:
    """Header values become a JSON-compatible dict with entries in source order."""
    metadata = FileMetadata(
        format="gpi",
        version="2.0",
        generated_by="TestDB",
        date_generated=date(2026, 9, 29),
        entries=(MetadataEntry("b", "2"), MetadataEntry("a", "1")),
    )

    assert header_metadata(metadata) == {
        "version": "2.0",
        "generated_by": "TestDB",
        "date_generated": "2026-09-29",
        "entries": [["b", "2"], ["a", "1"]],
    }
