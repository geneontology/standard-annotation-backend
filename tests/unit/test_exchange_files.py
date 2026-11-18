"""Verify reading GPI and GPAD documents into records and rejected rows."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from go_standard_annotation_schema.io import GpadReader, GpiReader, Record

from standard_annotation_backend.domain.jobs import JobFailureCode
from standard_annotation_backend.domain.refresh import RowRejection
from standard_annotation_backend.exchange_files import (
    ExchangeDocument,
    ExchangeHeaderError,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
GPI_HEADER = "!gpi-version: 2.0\n!generated-by: TestDB\n!date-generated: 2026-09-29\n"
GPAD_HEADER = "!gpad-version: 2.0\n!generated-by: TestDB\n!date-generated: 2026-09-29\n"
GPI_ROW = (
    "UniProtKB:P1\tSYM\tProtein\t\tSO:0001217\tNCBITaxon:9606\t\tUniProtKB:P1\t\t\t"
)
GPAD_ROW = (
    "UniProtKB:P12345\t\tRO:0002331\tGO:0008150\tPMID:1\tECO:0000314"
    "\t\t\t2026-09-29\tMGI\t\t"
)
NUL_REJECTION = RowRejection(4, "syntax", "contains a NUL character")


def _gpi_items(text: str) -> Sequence[object]:
    """Return every item that `ExchangeDocument` yields for a GPI document."""
    with ExchangeDocument(GpiReader, text) as document:
        return list(document.items())


def _gpad_items(text: str) -> Sequence[object]:
    """Return every item that `ExchangeDocument` yields for a GPAD document."""
    with ExchangeDocument(GpadReader, text) as document:
        return list(document.items())


@dataclass(frozen=True, slots=True)
class Format:
    """One exchange file format, with sample text for the shared tests."""

    read: Callable[[str], Sequence[object]]
    header: str
    row: str
    invalid_row: str
    invalid_field: str
    columns: int


FORMATS = [
    pytest.param(
        Format(
            _gpi_items,
            GPI_HEADER,
            GPI_ROW,
            GPI_ROW.replace("SYM", "A raw symbol"),
            "db_object_symbol",
            11,
        ),
        id="gpi",
    ),
    pytest.param(
        Format(
            _gpad_items,
            GPAD_HEADER,
            GPAD_ROW,
            GPAD_ROW.replace("2026-09-29", "bad"),
            "annotation_date",
            12,
        ),
        id="gpad",
    ),
]


@pytest.mark.parametrize("fmt", FORMATS)
def test_invalid_rows_become_rejections_in_line_order(fmt: Format) -> None:
    """Rows the reader cannot load become rejections, in line order with valid rows.

    A row with the wrong number of fields is `field-count`, and a row with an
    invalid value is `validation`, with a reason that starts with the field name.
    """
    text = fmt.header + fmt.row + "\n" + "too\tfew\n" + fmt.invalid_row + "\n"

    items = fmt.read(text)

    assert isinstance(items[0], Record) and items[0].line_number == 4
    assert items[1] == RowRejection(
        5, "field-count", f"expected {fmt.columns} fields, found 2"
    )
    assert isinstance(items[2], RowRejection)
    assert (items[2].line_number, items[2].code) == (6, "validation")
    assert items[2].reason.startswith(f"{fmt.invalid_field}:")


def test_validation_reasons_are_cut_to_1000_characters() -> None:
    """A reason that would repeat a long rejected value is cut to 1,000 characters."""
    [rejection] = _gpi_items(GPI_HEADER + GPI_ROW.replace("SYM", "x " * 1000) + "\n")

    assert isinstance(rejection, RowRejection)
    assert rejection.code == "validation"
    assert len(rejection.reason) == 1000


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_data_line_with_nul_is_one_syntax_rejection(fmt: Format) -> None:
    """A data line containing a NUL character is one `syntax` rejection.

    PostgreSQL text columns cannot store the NUL character.
    """
    row = fmt.row.replace("UniProtKB:P1", "UniProtKB:P\x001", 1)

    assert fmt.read(fmt.header + row + "\n") == [NUL_REJECTION]


@pytest.mark.parametrize("column", range(11))
def test_nul_in_any_gpi_column_rejects_the_row(column: int) -> None:
    """A NUL character in any GPI column rejects the row, whatever the column holds."""
    fields = GPI_ROW.split("\t")
    fields[column] = ("db-subset=value" if column == 10 else fields[column]) + "\x00"

    assert _gpi_items(GPI_HEADER + "\t".join(fields) + "\n") == [NUL_REJECTION]


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_header_line_with_nul_fails_the_document(fmt: Format) -> None:
    """A NUL character in a header line fails the whole document with `header`."""
    text = fmt.header + "!custom: va\x00lue\n" + fmt.row + "\n"

    with pytest.raises(ExchangeHeaderError) as raised:
        fmt.read(text)

    assert raised.value.message == "line 4 contains a NUL character"
    assert raised.value.failure_code is JobFailureCode.HEADER
    assert raised.value.failure_details == {"message": raised.value.message}


@pytest.mark.parametrize(
    ("read", "text"),
    [
        pytest.param(_gpad_items, GPAD_ROW + "\n", id="gpad-missing-header"),
        pytest.param(_gpad_items, GPAD_HEADER.replace("2.0", "1.2"), id="gpad-1.2"),
        pytest.param(_gpi_items, GPI_HEADER.replace("2.0", "1.2"), id="gpi-1.2"),
        pytest.param(
            _gpi_items,
            "!gpi-version: 2.0\n!date-generated: 2026-09-29\n",
            id="gpi-missing-generated-by",
        ),
    ],
)
def test_an_invalid_header_fails_with_a_short_message(
    read: Callable[[str], Sequence[object]], text: str
) -> None:
    """A missing, incomplete, or non-2.0 header fails the document with `header`.

    The message is at most 1,000 characters, and the error's text does not repeat
    the document.
    """
    with pytest.raises(ExchangeHeaderError) as raised:
        read(text)

    assert 0 < len(raised.value.message) <= 1000
    assert raised.value.failure_details == {"message": raised.value.message}
    assert text not in str(raised.value)


def test_gpad_rows_with_alternatives_expand_into_one_record_each() -> None:
    """A with/from column with two alternatives yields two records on one line.

    The row counts once in `data_rows`, and the header values are kept as metadata.
    """
    text = (FIXTURES / "gpad" / "minimal.gpad").read_text()

    with ExchangeDocument(GpadReader, text) as document:
        items = list(document.items())
        data_rows = document.data_rows
        metadata = document.metadata

    assert all(isinstance(item, Record) for item in items)
    assert [item.line_number for item in items] == [7, 8, 8]
    assert data_rows == 2
    assert metadata["generated_by"] == "TestDB"
    entries = metadata["entries"]
    assert isinstance(entries, list)
    assert ["project-name", "Standard Annotation Backend"] in entries


def test_gpad_row_with_a_non_default_evidence_term_is_rejected() -> None:
    """A row whose evidence is not one of the 26 default ECO terms is rejected.

    The reason names `evidence_type`, so the report shows what to fix.
    """
    [item] = _gpad_items(
        GPAD_HEADER + GPAD_ROW.replace("ECO:0000314", "ECO:0006051") + "\n"
    )

    assert isinstance(item, RowRejection)
    assert (item.line_number, item.code) == (4, "validation")
    assert item.reason.startswith("evidence_type:")


def test_gpi_record_keeps_the_validated_entity_its_line_and_the_header() -> None:
    """A GPI record holds the validated entity and its line number.

    The line number counts header, comment, and blank lines, and the header
    values are kept as metadata.
    """
    text = (FIXTURES / "gpi" / "minimal.gpi").read_text()

    with ExchangeDocument(GpiReader, text) as document:
        [record] = list(document.items())
        metadata = document.metadata

    assert isinstance(record, Record)
    assert record.line_number == 7
    assert record.item.model_dump(mode="json") == {
        "db_object_id": "UniProtKB:P12345",
        "db_object_symbol": "ABC1",
        "db_object_name": "Example protein",
        "db_object_synonyms": ["ABC1", "ALIAS"],
        "db_object_type": "SO:0001217",
        "db_object_taxon_id": "NCBITaxon:9606",
        "encoded_by": None,
        "canonical_object_id": "UniProtKB:P12345",
        "protein_containing_complex_members": None,
        "db_xrefs": ["UniProtKB:P12345"],
        "gene_product_properties": None,
    }
    assert metadata == {
        "version": "2.0",
        "generated_by": "TestDB",
        "date_generated": "2026-09-29",
        "entries": [
            ["gpi-version", "2.0"],
            ["generated-by", "TestDB"],
            ["date-generated", "2026-09-29"],
            ["project-name", "Standard Annotation Backend"],
        ],
    }


def test_gpi_header_values_followed_by_whitespace_are_read_without_it() -> None:
    """Tabs or spaces after a header value are not part of the value.

    WormBase writes `!gpi-version: 2.0` followed by tabs.
    """
    text = (
        "!gpi-version: 2.0\t\t\t\n!generated-by: WB\t\t\t\t\n"
        "!date-generated: 2026-11-10 \n"
    )

    with ExchangeDocument(GpiReader, text) as document:
        metadata = document.metadata

    assert (
        metadata["version"],
        metadata["generated_by"],
        metadata["date_generated"],
    ) == ("2.0", "WB", "2026-11-10")
