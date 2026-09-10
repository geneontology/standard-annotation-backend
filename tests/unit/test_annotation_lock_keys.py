"""Tests for the PostgreSQL locks used to coordinate annotation writes."""

from typing import Any, cast

import pytest

from standard_annotation_backend.persistence.locks import (
    acquire_global_annotation_write_lock,
    acquire_signature_locks,
    ordered_signature_lock_keys,
    signature_lock_key,
)


class RecordingSession:
    """Collect SQL calls made by a lock helper.

    This small test double lets unit tests inspect a helper's database request
    without opening a PostgreSQL connection.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, int]]] = []

    def execute(self, statement: Any, parameters: dict[str, int]) -> None:
        self.calls.append((str(statement), parameters))


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


def test_global_lock_uses_parameterized_shared_sql_by_default() -> None:
    session = RecordingSession()

    acquire_global_annotation_write_lock(cast(Any, session))

    assert session.calls == [
        (
            "SELECT pg_advisory_xact_lock_shared(:lock_key)",
            {"lock_key": -(2**63)},
        )
    ]


def test_global_lock_can_be_acquired_exclusively() -> None:
    session = RecordingSession()

    acquire_global_annotation_write_lock(cast(Any, session), exclusive=True)

    assert session.calls == [
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {"lock_key": -(2**63)},
        )
    ]


def test_signature_locks_use_parameterized_exclusive_sql_in_sorted_order() -> None:
    forward_session = RecordingSession()
    reverse_session = RecordingSession()
    low = "0123456789abcdef" * 4
    high = "f123456789abcdef" * 4

    acquire_signature_locks(cast(Any, forward_session), [low, high, low])
    acquire_signature_locks(cast(Any, reverse_session), [high, low, high])

    expected_calls = [
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {"lock_key": 81985529216486895},
        ),
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {"lock_key": 8152436061464415727},
        ),
    ]
    assert forward_session.calls == expected_calls
    assert reverse_session.calls == expected_calls
