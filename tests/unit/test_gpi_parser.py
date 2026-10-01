"""Verify strict parsing of GPI 2.0 source documents."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from standard_annotation_backend.domain.entities import EntitySource
from standard_annotation_backend.entity_sources.http import EntitySourceDocument
from standard_annotation_backend.gpi.parser import GpiParseError, parse_gpi

FIXTURES = Path(__file__).parents[1] / "fixtures" / "gpi"


def _document(name: str, *, text: str | None = None) -> EntitySourceDocument:
    """Build an `EntitySourceDocument` from a fixture file, or from `text` if given."""
    source_text = (FIXTURES / name).read_text() if text is None else text
    return EntitySourceDocument(
        source_key="fixture",
        source_url=f"fixture:{name}",
        source_checksum=sha256(source_text.encode()).hexdigest(),
        fetched_at=datetime(2026, 9, 29, tzinfo=UTC),
        text=source_text,
    )


def test_parse_gpi_retains_metadata_and_validated_schema_entity() -> None:
    """A valid GPI 2.0 document yields a catalog with its source details and metadata.

    The first record holds the validated schema entity and its line number.
    """
    document = _document("minimal.gpi")
    catalog = parse_gpi(document)

    assert catalog.source == EntitySource(
        source_url=document.source_url,
        source_checksum=document.source_checksum,
        fetched_at=document.fetched_at,
    )
    assert catalog.source_format == "gpi-2.0"
    assert catalog.source_metadata == {
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
    assert catalog.warnings == ()
    assert catalog.records[0].line_number == 7
    assert catalog.records[0].entity.model_dump(mode="json") == {
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


def test_parse_gpi_accepts_a_header_only_document() -> None:
    """A complete GPI header can represent an empty catalog."""
    catalog = parse_gpi(_document("empty.gpi"))

    assert catalog.records == ()
    assert catalog.source_metadata["generated_by"] == "TestDB"
    assert catalog.warnings == ()


def test_parse_gpi_records_physical_line_numbers_for_repeated_ids() -> None:
    """Repeated IDs remain distinct records identified by their source lines."""
    catalog = parse_gpi(_document("repeated-id.gpi"))

    assert [record.line_number for record in catalog.records] == [5, 8]
    assert [record.entity.db_object_id for record in catalog.records] == [
        "UniProtKB:P12345",
        "UniProtKB:P12345",
    ]
    assert [record.entity.db_object_symbol for record in catalog.records] == [
        "ABC1",
        "ABC1-2",
    ]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (
            "GPI 1.2",
            "!gpi-version: 1.2\n!generated-by: TestDB\n!date-generated: 2026-09-29\n",
        ),
        (
            "missing generated-by header",
            "!gpi-version: 2.0\n!date-generated: 2026-09-29\n",
        ),
    ],
)
def test_parse_gpi_translates_invalid_headers_to_a_safe_error(
    name: str, text: str
) -> None:
    """Invalid or incomplete headers raise `GpiParseError` with the code `header`.

    The error details describe the problem, and the error message does not include
    the document text.
    """
    with pytest.raises(GpiParseError) as raised:
        parse_gpi(_document("invalid-header.gpi", text=text))

    assert raised.value.code == "header", name
    assert text not in str(raised.value)
    assert raised.value.message, name
    assert raised.value.details == {"message": raised.value.message}


HEADER = "!gpi-version: 2.0\n!generated-by: TestDB\n!date-generated: 2026-09-29\n"


def _row(identifier: str, symbol: str = "SYM") -> str:
    """Return one GPI data row with the given identifier and symbol."""
    return (
        f"{identifier}\t{symbol}\tProtein\t\tSO:0001217\tNCBITaxon:9606\t\t"
        f"{identifier}\t\t\t\n"
    )


def test_parse_gpi_reports_every_invalid_row() -> None:
    """Every invalid row is reported with its line number, field, and reason.

    Valid rows around them do not stop the report, and the document still fails
    as a whole.
    """
    text = (
        HEADER
        + _row("UniProtKB:P1")
        + _row("UniProtKB:BAD", symbol="A raw rejected symbol")
        + "UniProtKB:SHORT\ttoo\tfew\tcolumns\n"
        + _row("UniProtKB:P2")
    )

    with pytest.raises(GpiParseError) as raised:
        parse_gpi(_document("invalid-rows.gpi", text=text))

    error = raised.value
    assert error.code == "row_validation"
    validation, field_count = error.issues
    [field] = validation.fields
    assert error.details == {
        "issue_count": 2,
        "issues": [
            {
                "line_number": 5,
                "category": "validation",
                "message": None,
                "fields": [
                    {
                        "field": "db_object_symbol",
                        "message": field.message,
                        "value": "A raw rejected symbol",
                    }
                ],
            },
            {
                "line_number": 6,
                "category": "field-count",
                "message": "expected 11 fields, found 4",
                "fields": [],
            },
        ],
    }
    assert "Invalid db_object_symbol format" in field.message
    assert field_count.line_number == 6


def test_parse_gpi_truncates_long_values_and_messages() -> None:
    """Reported values and messages are cut to 200 characters."""
    symbol = "x " * 300

    with pytest.raises(GpiParseError) as raised:
        parse_gpi(_document("long.gpi", text=HEADER + _row("UniProtKB:X", symbol)))

    [field] = raised.value.issues[0].fields
    assert field.value == symbol[:200]
    assert len(field.message) <= 200


def test_parse_gpi_reports_at_most_100_rows_with_the_total() -> None:
    """A document with many invalid rows reports the first 100 and the full count."""
    text = HEADER + "".join(f"UniProtKB:{index}\tshort\n" for index in range(150))

    with pytest.raises(GpiParseError) as raised:
        parse_gpi(_document("many.gpi", text=text))

    details = raised.value.details
    assert details["issue_count"] == 150
    assert details["issues"] == [
        issue.to_details() for issue in raised.value.issues[:100]
    ]
    assert [issue.line_number for issue in raised.value.issues[:100]] == list(
        range(4, 104)
    )


@pytest.mark.parametrize("column", range(11))
def test_parse_gpi_rejects_nul_in_every_entity_field(column: int) -> None:
    """A NUL character in any entity column is reported as an invalid row.

    PostgreSQL text columns cannot store NUL characters.
    """
    lines = (FIXTURES / "minimal.gpi").read_text().splitlines()
    fields = lines[-1].split("\t")
    fields[column] = ("db-subset=value" if column == 10 else fields[column]) + "\x00"
    text = "\n".join([*lines[:-1], "\t".join(fields)]) + "\n"

    with pytest.raises(GpiParseError) as caught:
        parse_gpi(_document("nul.gpi", text=text))

    assert caught.value.code == "row_validation"
    assert caught.value.details["issues"] == [
        {
            "line_number": len(lines),
            "category": "syntax",
            "message": "contains a NUL character",
            "fields": [],
        }
    ]
    assert "\x00" not in str(caught.value)


@pytest.mark.parametrize(
    "header",
    ["!generated-by: Test\x00DB", "!custom\x00key: value", "!custom: val\x00ue"],
)
def test_parse_gpi_rejects_nul_in_source_metadata(header: str) -> None:
    """A NUL character in a header key or value raises a `header` error."""
    text = (FIXTURES / "empty.gpi").read_text()
    if header.startswith("!generated-by"):
        text = text.replace("!generated-by: TestDB", header)
    else:
        text += header + "\n"
    with pytest.raises(GpiParseError) as caught:
        parse_gpi(_document("nul.gpi", text=text))
    assert caught.value.code == "header"
    assert caught.value.message is not None
    assert "NUL" in caught.value.message
    assert caught.value.details == {"message": caught.value.message}
