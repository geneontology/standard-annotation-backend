"""Tests for the namespaced PostgreSQL advisory-lock keys."""

from uuid import UUID

import pytest

from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    lock_key,
)

_SIGNED_64_BIT_MIN = -(2**63)
_SIGNED_64_BIT_MAX = 2**63 - 1


@pytest.mark.parametrize(
    "value",
    [None, "go", UUID("00000000-0000-0000-0000-000000000001")],
)
def test_lock_key_is_stable_and_fits_postgresql_lock_keys(
    value: str | UUID | None,
) -> None:
    """A namespace and value always map to the same signed 64-bit key."""
    for namespace in LockNamespace:
        key = lock_key(namespace, value)

        assert key == lock_key(namespace, value)
        assert _SIGNED_64_BIT_MIN <= key <= _SIGNED_64_BIT_MAX


def test_keyed_lock_values_are_unchanged_across_releases() -> None:
    """Keys stay fixed so workers running different releases exclude each other.

    During a rolling deployment, old and new workers can hold the same job,
    ontology, entity source, or group lock at once. They exclude each other only
    if both derive the same key.
    """
    assert lock_key(LockNamespace.ONTOLOGY, "go") == 2179758608860698013
    assert lock_key(LockNamespace.JOB, UUID(int=1)) == -3237178273019958173
    assert lock_key(LockNamespace.ENTITY_CATALOG, "mgi") == 2322029887830009188
    assert lock_key(LockNamespace.ANNOTATION_GROUP, "mgi") == 8030413906021117537


def test_same_value_in_different_namespaces_uses_different_keys() -> None:
    """Locks in one namespace do not block equally named locks in another."""
    keys = {lock_key(namespace, "go") for namespace in LockNamespace}
    namespace_wide = {lock_key(namespace) for namespace in LockNamespace}

    assert len(keys) == len(LockNamespace)
    assert len(namespace_wide) == len(LockNamespace)


def test_different_values_in_one_namespace_use_different_keys() -> None:
    """Locks for different values in a namespace do not block each other."""
    assert lock_key(LockNamespace.ANNOTATION_GROUP, "mgi") != lock_key(
        LockNamespace.ANNOTATION_GROUP, "zfin"
    )
    assert lock_key(LockNamespace.ANNOTATION_GROUP, "mgi") != lock_key(
        LockNamespace.ANNOTATION_GROUP
    )


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_lock_key_rejects_blank_values(value: str) -> None:
    """A blank value is rejected rather than silently sharing one lock."""
    with pytest.raises(ValueError, match="blank"):
        lock_key(LockNamespace.ENTITY_CATALOG, value)
