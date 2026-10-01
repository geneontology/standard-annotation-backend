"""Provide PostgreSQL advisory locks for concurrent application operations."""

import hashlib
import string
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.ontology import OntologyKey

GLOBAL_ANNOTATION_WRITE_LOCK_KEY = -(2**63)
_AUTHORIZATION_REFRESH_LOCK_KEY = 0x53414241555448
_MAX_SIGNATURE_LOCK_KEY = (1 << 63) - 1
_HEXADECIMAL_CHARACTERS = frozenset(string.hexdigits)
_REFRESH_START_LOCK_KEY = 0x5341425253545254  # "SABRSTRT"
_JOB_LOCK_PERSON = b"SABJOB"
_ONTOLOGY_LOCK_PERSON = b"SABONTO"
_ENTITY_LOCK_PERSON = b"SABENT"


def acquire_authorization_refresh_lock(session: Session) -> None:
    """Prevent concurrent authorization replacements.

    The transaction holds the `SABAUTH` advisory lock from the duplicate-source
    check through the replacement. A second refresh waits until the
    first transaction commits or rolls back.

    Args:
        session: Session whose transaction owns the lock.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _AUTHORIZATION_REFRESH_LOCK_KEY},
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


def ontology_lock_key(key: str) -> int:
    """Return a stable PostgreSQL advisory-lock key for an ontology key."""
    digest = hashlib.blake2b(
        key.encode("utf-8"), digest_size=8, person=_ONTOLOGY_LOCK_PERSON
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def entity_catalog_lock_key(source_key: str) -> int:
    """Return the PostgreSQL advisory lock key for one entity source.

    The key is a 64-bit hash of the source key. A BLAKE2 `person` value that only
    this function uses keeps the keys apart from job and ontology lock keys. If
    two sources ever hash to the same key, their publications only wait for each
    other.

    Raises:
        ValueError: If the source key is blank.
    """
    if not source_key.strip():
        raise ValueError("source key must not be blank")
    digest = hashlib.blake2b(
        source_key.encode("utf-8"), digest_size=8, person=_ENTITY_LOCK_PERSON
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def acquire_entity_catalog_lock(session: Session, source_key: str) -> None:
    """Make catalog publications and retirements for one entity source run in turn.

    The transaction holds the lock until it commits or rolls back, so only one
    transaction at a time can replace or retire a source's active catalog.
    Fetching, parsing, staging, and work for other sources are not blocked.

    Args:
        session: Session whose transaction owns the lock.
        source_key: Entity source whose catalog changes must not overlap.

    Raises:
        ValueError: If the source key is blank.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": entity_catalog_lock_key(source_key)},
    )


@contextmanager
def ontology_refresh_lock(engine: Engine, key: OntologyKey | str) -> Iterator[bool]:
    """Try to lock ontology refreshing for one key until the context exits.

    The lock is not waited for. If another connection holds it, the context
    yields `False` at once, and the caller decides whether to retry later.

    Args:
        engine: Database engine used to own the dedicated lock connection.
        key: Ontology whose refreshes must not overlap.

    Yields:
        `True` when this connection owns the lock, otherwise `False`.

    Raises:
        RuntimeError: If PostgreSQL reports that an owned lock was not released.
    """
    key_value = key.value if isinstance(key, OntologyKey) else key
    lock_key = ontology_lock_key(key_value)
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
                    raise RuntimeError("ontology refresh lock was not released")


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


def acquire_refresh_start_lock(session: Session) -> None:
    """Make refresh job creation run one request at a time, for every kind.

    Starting a refresh first looks for a queued or running job for each source
    and creates one only when none exists. Without this lock, two requests
    running at once could both find no job and both create one.

    One lock covers every kind. Starts are short and rare, so serializing them
    costs nothing noticeable, and a single key cannot deadlock with itself.

    Args:
        session: Session whose transaction owns the lock.
    """
    # A transaction-level lock: PostgreSQL releases it when the caller's
    # transaction commits or rolls back, so it cannot outlive the job creation.
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _REFRESH_START_LOCK_KEY},
    )
