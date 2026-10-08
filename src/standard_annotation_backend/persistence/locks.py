"""Provide PostgreSQL advisory locks for concurrent application operations."""

import hashlib
import string
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from enum import Enum
from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

GLOBAL_ANNOTATION_WRITE_LOCK_KEY = -(2**63)
_MAX_SIGNATURE_LOCK_KEY = (1 << 63) - 1
_HEXADECIMAL_CHARACTERS = frozenset(string.hexdigits)


class LockNamespace(Enum):
    """Name a family of advisory locks that serialize one kind of operation.

    Each member's value is the BLAKE2b `person` parameter used to derive its lock
    keys, so equal values in different namespaces get unrelated keys. The values
    must not change: workers from different releases exclude each other only if
    they derive the same keys.

    Attributes:
        JOB: One job's execution, keyed by job ID.
        ONTOLOGY: Staging, activation, and pruning of one ontology, keyed by
            ontology key.
        ENTITY_CATALOG: Publication and retirement of one entity source's
            catalog, keyed by source key.
        ANNOTATION_GROUP: GPAD staging and publication for one group, keyed by
            group key.
        AUTHORIZATION_REFRESH: Replacement of users and grants. Used without a
            value.
        REFRESH_START: Creation of refresh jobs of every kind. Used without a
            value.
    """

    JOB = b"SABJOB"
    ONTOLOGY = b"SABONTO"
    ENTITY_CATALOG = b"SABENT"
    ANNOTATION_GROUP = b"SABGPAD"
    AUTHORIZATION_REFRESH = b"SABAUTH"
    REFRESH_START = b"SABRSTRT"


def lock_key(namespace: LockNamespace, value: str | UUID | None = None) -> int:
    """Return the PostgreSQL advisory-lock key for one value in a namespace.

    PostgreSQL's one-argument advisory-lock functions take a signed 64-bit
    integer. The key is an 8-byte BLAKE2b digest of the value, personalized with
    the namespace, which gives a stable, well-distributed mapping in every
    process. The hash is used for key generation, not for security. A collision
    is unlikely and would only make two unrelated operations run one after the
    other.

    Args:
        namespace: Family of locks the key belongs to.
        value: What to lock within the namespace. `None` locks the whole
            namespace.

    Returns:
        A stable signed 64-bit advisory-lock key.

    Raises:
        ValueError: If `value` is a blank string.
    """
    if value is None:
        data = b""
    elif isinstance(value, UUID):
        data = value.bytes
    elif not value.strip():
        raise ValueError("lock value must not be blank")
    else:
        data = value.encode("utf-8")
    digest = hashlib.blake2b(data, digest_size=8, person=namespace.value).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@contextmanager
def try_advisory_lock(
    engine: Engine, namespace: LockNamespace, value: str | UUID | None = None
) -> Iterator[bool]:
    """Try to take an advisory lock and hold it until the context exits.

    The lock is not waited for. If another connection holds it, the context
    yields `False` at once and the caller decides whether to retry later. A
    dedicated connection owns the lock for the whole context, so it can span
    several transactions. If that connection is lost, for example because the
    worker process dies, PostgreSQL releases the lock.

    Args:
        engine: Database engine used to open the dedicated lock connection.
        namespace: Family of locks the lock belongs to.
        value: What to lock within the namespace. `None` locks the whole
            namespace.

    Yields:
        `True` when this connection owns the lock, otherwise `False`.

    Raises:
        ValueError: If `value` is a blank string.
        RuntimeError: If PostgreSQL reports that an owned lock was not released.
    """
    key = lock_key(namespace, value)
    with engine.connect() as connection:
        acquired = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": key}
            )
        )
        try:
            yield acquired
        finally:
            if acquired:
                released = connection.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": key}
                )
                if released is not True:
                    raise RuntimeError(
                        f"{namespace.name.lower()} advisory lock was not released"
                    )


def acquire_transaction_lock(
    session: Session, namespace: LockNamespace, value: str | UUID | None = None
) -> None:
    """Wait for an advisory lock and hold it until the session's transaction ends.

    PostgreSQL releases the lock when the transaction commits or rolls back, so
    it cannot outlive the work it protects.

    Args:
        session: Session whose transaction owns the lock.
        namespace: Family of locks the lock belongs to.
        value: What to lock within the namespace. `None` locks the whole
            namespace.

    Raises:
        ValueError: If `value` is a blank string.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key(namespace, value)},
    )


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


class AdvisoryTryLock(Protocol):
    """Try to take a namespaced advisory lock for the duration of a context."""

    def __call__(
        self, namespace: LockNamespace, value: str | UUID | None = None
    ) -> AbstractContextManager[bool]:
        """Return a context manager that yields whether the lock was acquired."""
        ...


def bind_try_lock(engine: Engine) -> AdvisoryTryLock:
    """Return a try-lock that opens its dedicated connections from `engine`.

    The result behaves like `try_advisory_lock` with `engine` already supplied,
    so callers can take locks without depending on a database engine.
    """

    def try_lock(
        namespace: LockNamespace, value: str | UUID | None = None
    ) -> AbstractContextManager[bool]:
        return try_advisory_lock(engine, namespace, value)

    return try_lock
