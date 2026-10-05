"""Verify reading GPAD documents into annotations and rejected records."""

import gzip
from pathlib import Path

import pytest

from standard_annotation_backend.domain.annotation_management import (
    AnnotationRejection,
)
from standard_annotation_backend.domain.refresh import RefreshFailureCode
from standard_annotation_backend.gpad.parser import (
    GpadDocument,
    GpadHeaderError,
    ParsedAnnotation,
)
from standard_annotation_backend.refresh.decoding import decode_source_text
from standard_annotation_backend.refresh.fetchers import SourceError

MINIMAL = (Path(__file__).parents[1] / "fixtures" / "gpad" / "minimal.gpad").read_text()
HEADER = "!gpad-version: 2.0\n!generated-by: TestDB\n!date-generated: 2026-09-29\n"
VALID = (
    "UniProtKB:P12345\t\tRO:0002331\tGO:0008150\tPMID:1\tECO:0000314"
    "\t\t\t2026-09-29\tMGI\t\t"
)


def _items(text: str) -> list[ParsedAnnotation | AnnotationRejection]:
    with GpadDocument(text) as document:
        return list(document.items())


def test_rows_with_alternatives_expand_into_one_annotation_each() -> None:
    """A with/from column with two alternatives yields two annotations on one line.

    The row counts once in `data_rows`, and the header values are kept as metadata.
    """
    with GpadDocument(MINIMAL) as document:
        items = list(document.items())
        data_rows = document.data_rows
        metadata = document.metadata

    assert [(item.line_number, type(item).__name__) for item in items] == [
        (7, "ParsedAnnotation"),
        (8, "ParsedAnnotation"),
        (8, "ParsedAnnotation"),
    ]
    assert data_rows == 2
    assert metadata["generated_by"] == "TestDB"
    entries = metadata["entries"]
    assert isinstance(entries, list)
    assert ["project-name", "Standard Annotation Backend"] in entries


def test_invalid_rows_become_rejections_in_line_order() -> None:
    """Unreadable rows become rejections in line order with a short reason."""
    text = (
        HEADER + VALID + "\n" + "too\tfew\n" + VALID.replace("2026-09-29", "bad") + "\n"
    )

    items = _items(text)

    assert isinstance(items[0], ParsedAnnotation)
    assert isinstance(items[1], AnnotationRejection)
    assert items[1] == AnnotationRejection(5, "field-count", items[1].reason)
    assert isinstance(items[2], AnnotationRejection)
    assert (items[2].line_number, items[2].code) == (6, "validation")
    assert items[2].reason.startswith("annotation_date:")
    rejections = [item for item in items if isinstance(item, AnnotationRejection)]
    assert all(len(rejection.reason) <= 200 for rejection in rejections)


def test_nul_data_line_is_rejected_and_nul_header_is_a_header_error() -> None:
    """A data line with NUL is one `syntax` rejection; a NUL header line fails."""
    items = _items(HEADER + VALID.replace("PMID:1", "PMID:\x001") + "\n")

    assert items == [AnnotationRejection(4, "syntax", "contains a NUL character")]
    with pytest.raises(GpadHeaderError):
        _items("!gpad-version: 2.0\x00\n")


@pytest.mark.parametrize(
    "text",
    [
        VALID + "\n",
        "!gpad-version: 1.2\n!generated-by: X\n!date-generated: 2026-09-29\n",
    ],
    ids=["missing-header", "wrong-version"],
)
def test_invalid_header_raises_with_a_short_message(text: str) -> None:
    """A missing or non-2.0 header fails the whole document with a short message."""
    with pytest.raises(GpadHeaderError) as raised:
        _items(text)

    assert 0 < len(raised.value.message) <= 200


def test_decoding_expands_gzip_and_rejects_bad_bytes() -> None:
    """Gzip content is expanded; invalid gzip and UTF-8 fail with their own codes."""
    assert decode_source_text(gzip.compress(b"abc")) == "abc"
    with pytest.raises(SourceError) as bad_gzip:
        decode_source_text(b"\x1f\x8bnot gzip")
    with pytest.raises(SourceError) as bad_utf8:
        decode_source_text(b"\xff")

    assert bad_gzip.value.code is RefreshFailureCode.INVALID_GZIP
    assert bad_utf8.value.code is RefreshFailureCode.INVALID_UTF8
