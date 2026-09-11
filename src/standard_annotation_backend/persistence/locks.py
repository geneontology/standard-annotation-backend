"""Coordinate annotation writes with PostgreSQL transaction locks."""

import string
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

GLOBAL_ANNOTATION_WRITE_LOCK_KEY = -(2**63)
_MAX_SIGNATURE_LOCK_KEY = (1 << 63) - 1
_HEXADECIMAL_CHARACTERS = frozenset(string.hexdigits)


def signature_lock_key(signature: str) -> int:
    """Convert an annotation signature to a PostgreSQL advisory-lock key.

    Args:
        signature: A 64-character hexadecimal SHA-256 signature.

    Returns:
        A nonnegative integer that fits in PostgreSQL's signed 64-bit lock key.

    Raises:
        ValueError: If `signature` is not exactly 64 hexadecimal characters.
    """
    if len(signature) != 64 or not set(signature) <= _HEXADECIMAL_CHARACTERS:
        raise ValueError("signature must be exactly 64 hexadecimal characters")

    raw_key = int.from_bytes(bytes.fromhex(signature)[:8], byteorder="big")
    return raw_key & _MAX_SIGNATURE_LOCK_KEY


def ordered_signature_lock_keys(signatures: Iterable[str]) -> tuple[int, ...]:
    """Build the ordered lock-key list used for annotation signatures.

    Args:
        signatures: Annotation signatures that need to be locked.

    Returns:
        Unique lock keys in numeric order. Using one order prevents deadlocks
        when concurrent writes need the same keys.

    Raises:
        ValueError: If any signature is malformed.
    """
    return tuple(sorted({signature_lock_key(signature) for signature in signatures}))


def acquire_global_annotation_write_lock(
    session: Session,
    *,
    exclusive: bool = False,
) -> None:
    """Lock annotation writes until the current transaction ends.

    Args:
        session: Session whose transaction owns the lock.
        exclusive: Whether to block both ordinary writes and other exclusive
            operations. The default shared lock allows ordinary writes to run
            concurrently.
    """
    statement = (
        "SELECT pg_advisory_xact_lock(:lock_key)"
        if exclusive
        else "SELECT pg_advisory_xact_lock_shared(:lock_key)"
    )
    session.execute(
        text(statement),
        {"lock_key": GLOBAL_ANNOTATION_WRITE_LOCK_KEY},
    )


def acquire_signature_locks(session: Session, signatures: Iterable[str]) -> None:
    """Lock annotation signatures until the current transaction ends.

    Args:
        session: Session whose transaction owns the locks.
        signatures: Annotation signatures that need exclusive locks.

    Raises:
        ValueError: If any signature is malformed.
    """
    statement = text("SELECT pg_advisory_xact_lock(:lock_key)")
    for lock_key in ordered_signature_lock_keys(signatures):
        session.execute(statement, {"lock_key": lock_key})
