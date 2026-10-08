"""Tests for the PostgreSQL locks used to coordinate annotation writes."""

import pytest

from standard_annotation_backend.persistence.locks import (
    ordered_signature_lock_keys,
    signature_lock_key,
)


def test_signature_lock_key_is_deterministic_and_signed_63_bit() -> None:
    signature = "0123456789abcdef" * 4

    first = signature_lock_key(signature)

    assert first == 81985529216486895
    assert signature_lock_key(signature) == first
    assert 0 <= first <= (1 << 63) - 1
    assert signature_lock_key("f123456789abcdef" * 4) == 8152436061464415727
    assert signature_lock_key("f123456789abcdef" * 4) != first


@pytest.mark.parametrize(
    "signature",
    [
        "",
        "0" * 63,
        "0" * 65,
        "g" * 64,
        "00" * 31 + "zz",
    ],
)
def test_signature_lock_key_rejects_malformed_signatures(signature: str) -> None:
    with pytest.raises(ValueError):
        signature_lock_key(signature)


def test_ordered_signature_lock_keys_deduplicates_and_numeric_sorts() -> None:
    low = "0123456789abcdef" * 4
    high = "f123456789abcdef" * 4

    assert ordered_signature_lock_keys([high, low, high]) == (
        81985529216486895,
        8152436061464415727,
    )
