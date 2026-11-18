"""Verify the rule that an entity catalog lists each identifier on one line."""

from go_standard_annotation_schema.datamodel import Entity
from go_standard_annotation_schema.io import Record

from standard_annotation_backend.domain.entities import reject_repeated_identifiers
from standard_annotation_backend.domain.refresh import RowRejection


def _record(line_number: int, identifier: str) -> Record[Entity]:
    """Return a valid entity record for `identifier` on `line_number`."""
    return Record(
        item=Entity(
            db_object_id=identifier,
            db_object_type="SO:0001217",
            db_object_taxon_id="NCBITaxon:9606",
            canonical_object_id=identifier,
        ),
        line_number=line_number,
    )


def test_every_line_of_a_repeated_identifier_is_rejected() -> None:
    """Every line that lists a repeated identifier is rejected as `duplicate`.

    Each reason names the first other line with the identifier, and records with
    other identifiers are kept.
    """
    unique = _record(7, "MGI:2")
    records = (_record(5, "MGI:1"), unique, _record(9, "MGI:1"), _record(12, "MGI:1"))

    kept, rejections = reject_repeated_identifiers(records)

    assert kept == (unique,)
    assert rejections == (
        RowRejection(5, "duplicate", "db_object_id also appears on line 9"),
        RowRejection(9, "duplicate", "db_object_id also appears on line 5"),
        RowRejection(12, "duplicate", "db_object_id also appears on line 5"),
    )
