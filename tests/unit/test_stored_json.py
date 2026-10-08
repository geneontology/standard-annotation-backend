"""Tests for reading and writing stored JSON through `StoredJson`."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

import pytest
from pydantic import Field

from standard_annotation_backend.domain.stored_json import StoredDataError, StoredJson


class _Color(StrEnum):
    RED = "red"


@dataclass(frozen=True, slots=True)
class _Child:
    name: str


@dataclass(frozen=True, slots=True)
class _Stored:
    item_id: UUID
    at: datetime
    color: _Color
    count: Annotated[int, Field(ge=0)]
    tags: tuple[str, ...]
    children: tuple[_Child, ...]
    note: str | None = None


_STORED = StoredJson(_Stored, label="test value")
_VALUE = _Stored(
    item_id=UUID("00000000-0000-0000-0000-000000000001"),
    at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    color=_Color.RED,
    count=2,
    tags=("a", "b"),
    children=(_Child("x"),),
)
_DUMPED = {
    "item_id": "00000000-0000-0000-0000-000000000001",
    "at": "2026-01-02T03:04:05Z",
    "color": "red",
    "count": 2,
    "tags": ["a", "b"],
    "children": [{"name": "x"}],
    "note": None,
}


def test_dump_writes_json_compatible_values() -> None:
    """Dumping converts UUIDs, datetimes, enums, tuples, and nested values to JSON."""
    assert _STORED.dump(_VALUE) == _DUMPED


def test_load_reads_back_what_dump_wrote() -> None:
    """A dumped value loads back equal to the original."""
    assert _STORED.load(_STORED.dump(_VALUE)) == _VALUE


def test_load_ignores_unknown_keys() -> None:
    """Extra keys in a stored value are ignored when it loads."""
    assert _STORED.load({**_DUMPED, "tag_count": 2}) == _VALUE


@pytest.mark.parametrize(
    "value",
    [
        {**_DUMPED, "count": True},
        {**_DUMPED, "count": "2"},
        {**_DUMPED, "count": 2.0},
        {**_DUMPED, "count": -1},
        {**_DUMPED, "color": "blue"},
        {key: item for key, item in _DUMPED.items() if key != "tags"},
        {**_DUMPED, "children": [{"name": 3}]},
        ["not", "a", "mapping"],
        None,
    ],
)
def test_load_rejects_values_without_the_stored_shape(value: object) -> None:
    """Wrong types, booleans or strings as counts, negatives, and gaps are rejected."""
    with pytest.raises(StoredDataError) as caught:
        _STORED.load(value)
    assert str(caught.value) == "stored test value is invalid"
    assert caught.value.__cause__ is None


def test_load_error_never_contains_stored_values() -> None:
    """The error names what was read but not the data, so it is safe to log."""
    with pytest.raises(StoredDataError) as caught:
        _STORED.load({**_DUMPED, "tags": "secret-source-text"})
    assert "secret-source-text" not in str(caught.value)
    assert "secret-source-text" not in repr(caught.value)


def test_load_rejects_values_that_are_not_json() -> None:
    """A value that cannot be written as JSON is reported like any invalid value."""
    with pytest.raises(StoredDataError):
        _STORED.load({**_DUMPED, "note": object()})
