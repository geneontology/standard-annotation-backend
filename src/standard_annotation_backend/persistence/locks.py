"""Provide PostgreSQL advisory locks for concurrent application operations."""

import hashlib
import string
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

GLOBAL_ANNOTATION_WRITE_LOCK_KEY = -(2**63)
_AUTHORIZATION_SYNC_LOCK_KEY = 0x53414241555448
_MAX_SIGNATURE_LOCK_KEY = (1 << 63) - 1
_HEXADECIMAL_CHARACTERS = frozenset(string.hexdigits)
_JOB_LOCK_PERSON = b"SABJOB"


def acquire_authorization_sync_lock(session: Session) -> None:
    """Prevent concurrent authorization replacements.

    The transaction holds the `SABAUTH` advisory lock from the duplicate-source
    check through the replacement. A second synchronization waits until the
    first transaction commits or rolls back.

    Args:
        session: Session whose transaction owns the lock.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _AUTHORIZATION_SYNC_LOCK_KEY},
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


def job_lock_key(job_id: UUID) -> int:
    """Convert a job UUID to a PostgreSQL advisory-lock key.

    PostgreSQL's one-argument advisory-lock functions accept a signed 64-bit
    integer, while a UUID contains 128 bits. BLAKE2b provides a deterministic,
    well-distributed 64-bit mapping that is stable across application processes.
    Its `SABJOB` parameter keeps this mapping separate from other BLAKE2b uses.
    This function uses the hash for stable key generation, not for security.

    Hashing cannot guarantee uniqueness. A collision is unlikely at the expected
    job volume and would only make two unrelated jobs run one after the other.

    Args:
        job_id: Durable job identifier to map into PostgreSQL's lock-key space.

    Returns:
        A stable signed 64-bit advisory-lock key.
    """
    digest = hashlib.blake2b(
        job_id.bytes,
        digest_size=8,
        person=_JOB_LOCK_PERSON,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@contextmanager
def job_execution_lock(engine: Engine, job_id: UUID) -> Iterator[bool]:
    """Try to lock one job for the duration of a worker operation.

    The context keeps a dedicated database connection open while it holds the
    PostgreSQL session-level advisory lock. If the worker loses that connection,
    PostgreSQL releases the lock. Celery can then redeliver the unacknowledged
    task message and resume the same `running` job. Redelivery sends the original
    task message again; it is different from an explicit Celery task retry.

    Celery documents task acknowledgement and redelivery at
    https://docs.celeryq.dev/en/stable/userguide/tasks.html and the relevant
    `task_acks_late` and `task_reject_on_worker_lost` settings at
    https://docs.celeryq.dev/en/stable/userguide/configuration.html.

    Args:
        engine: Database engine used to own the dedicated lock connection.
        job_id: Durable job whose execution must not overlap.

    Yields:
        `True` when this connection owns the lock, otherwise `False`.

    Raises:
        RuntimeError: If PostgreSQL reports that an owned lock was not released.
    """
    lock_key = job_lock_key(job_id)
    with engine.connect() as connection:
        acquired = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
        )
        try:
            yield acquired
        finally:
            if acquired:
                released = connection.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": lock_key},
                )
                if released is not True:
                    raise RuntimeError("job execution lock was not released")
