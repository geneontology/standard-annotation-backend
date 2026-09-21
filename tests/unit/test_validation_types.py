"""Test shared Pydantic validation types."""

import pytest
from pydantic import TypeAdapter, ValidationError

from standard_annotation_backend.validation_types import (
    NonBlankString,
    TrimmedNonBlankString,
)


def test_nonblank_string_preserves_surrounding_whitespace() -> None:
    """Nonblank free text retains whitespace supplied by the caller."""
    adapter = TypeAdapter(NonBlankString)

    assert adapter.validate_python("  supporting note  ") == "  supporting note  "

    with pytest.raises(ValidationError):
        adapter.validate_python(" \t\n ")


def test_trimmed_nonblank_string_normalizes_surrounding_whitespace() -> None:
    """Trimmed nonblank values discard surrounding whitespace."""
    adapter = TypeAdapter(TrimmedNonBlankString)

    assert adapter.validate_python("  group-1  ") == "group-1"

    with pytest.raises(ValidationError):
        adapter.validate_python(" \t\n ")
