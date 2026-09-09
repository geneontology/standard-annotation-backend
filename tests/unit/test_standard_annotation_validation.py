"""Tests for validating payloads with the Standard Annotation schema."""

from datetime import date
from uuid import UUID

import pytest

from standard_annotation_backend.domain.annotations import new_annotation_id
from standard_annotation_backend.domain.validation import validate_annotation


def valid_annotation_payload() -> dict[str, object]:
    """Return a minimal payload accepted by the Standard Annotation schema."""
    return {
        "db_object_id": "UniProtKB:P12345",
        "relation": "RO:0002331",
        "ontology_class_id": "GO:0008150",
        "references": ["PMID:1"],
        "evidence_type": "ECO:0000314",
        "annotation_date": "2026-09-09",
        "assigned_by": "GO_Central",
    }


def test_valid_payload_returns_schema_annotation() -> None:
    result = validate_annotation(valid_annotation_payload())

    assert result.is_valid
    assert result.errors == ()
    assert result.annotation is not None
    assert result.annotation.db_object_id == "UniProtKB:P12345"
    assert result.annotation.annotation_date == date(2026, 9, 9)


def test_invalid_payload_returns_structured_errors() -> None:
    payload = valid_annotation_payload()
    payload.pop("ontology_class_id")

    result = validate_annotation(payload)

    assert not result.is_valid
    assert result.annotation is None
    assert result.errors == (
        {
            "location": ("ontology_class_id",),
            "message": "Field required",
            "type": "missing",
        },
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("references", []),
        ("annotation_date", ["2026-09-09"]),
        ("annotation_properties", {"model_state": "not valid"}),
    ],
)
def test_schema_constraints_reject_invalid_values(field: str, value: object) -> None:
    payload = valid_annotation_payload()
    payload[field] = value

    result = validate_annotation(payload)

    assert not result.is_valid
    assert result.errors[0]["location"][0] == field


def test_extra_sab_identifier_is_rejected_by_core_schema() -> None:
    payload = valid_annotation_payload()
    payload["annotation_id"] = "018f6fa8-709b-7c13-9f75-4513f57b98c2"

    result = validate_annotation(payload)

    assert not result.is_valid
    assert result.annotation is None
    assert result.errors[0]["location"] == ("annotation_id",)
    assert result.errors[0]["type"] == "extra_forbidden"


def test_new_annotation_id_is_a_uuid() -> None:
    annotation_id = new_annotation_id()

    assert isinstance(annotation_id, UUID)
    assert UUID(str(annotation_id)) == annotation_id
