"""Tests for validating payloads with the Standard Annotation schema."""

from datetime import date

import pytest
from pydantic import BaseModel, ValidationError

from standard_annotation_backend.domain.validation import (
    field_path,
    validate_annotation,
    validation_issues,
)


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

    assert result.annotation is not None
    assert result.errors == ()
    assert result.annotation.db_object_id == "UniProtKB:P12345"
    assert result.annotation.annotation_date == date(2026, 9, 9)


def test_invalid_payload_returns_structured_errors() -> None:
    payload = valid_annotation_payload()
    payload.pop("ontology_class_id")

    result = validate_annotation(payload)

    assert result.annotation is None
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error["location"] == ("annotation", "ontology_class_id")
    assert error["type"] == "missing"
    assert isinstance(error["message"], str) and error["message"]


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

    assert result.annotation is None
    assert result.errors[0]["location"][:2] == ("annotation", field)


def test_extra_sab_identifier_is_rejected_by_core_schema() -> None:
    payload = valid_annotation_payload()
    payload["annotation_id"] = "018f6fa8-709b-7c13-9f75-4513f57b98c2"

    result = validate_annotation(payload)

    assert result.annotation is None
    assert result.errors[0]["location"] == ("annotation", "annotation_id")
    assert result.errors[0]["type"] == "extra_forbidden"


class _Nested(BaseModel):
    count: int


class _Outer(BaseModel):
    name: str
    nested: list[_Nested]


def test_validation_issues_keep_location_message_and_type_only() -> None:
    """Each Pydantic error becomes an issue with its location, message, and type."""
    with pytest.raises(ValidationError) as caught:
        _Outer.model_validate({"nested": [{"count": "secret-value"}]})

    issues = validation_issues(caught.value)

    assert [(issue["location"], issue["type"]) for issue in issues] == [
        (("name",), "missing"),
        (("nested", 0, "count"), "int_parsing"),
    ]
    assert all(
        isinstance(issue["message"], str) and issue["message"] for issue in issues
    )
    assert all(set(issue) == {"location", "message", "type"} for issue in issues)
    assert "secret-value" not in str(issues)


def test_validation_issues_place_a_root_before_each_location() -> None:
    """A supplied root names what was checked before each Pydantic location."""
    with pytest.raises(ValidationError) as caught:
        _Outer.model_validate({"nested": [{"count": "x"}]})

    issues = validation_issues(caught.value, root=("body",))

    assert [issue["location"] for issue in issues] == [
        ("body", "name"),
        ("body", "nested", 0, "count"),
    ]


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (("db_object_symbol",), "db_object_symbol"),
        (("extensions", 0, "term"), "extensions.0.term"),
        ((), "row"),
    ],
)
def test_field_path_joins_location_parts_or_names_the_row(
    location: tuple[str | int, ...], expected: str
) -> None:
    """A location becomes a dotted path, and an empty location names the row."""
    assert field_path(location) == expected
