"""Store and read the detail and data rows of entity catalog snapshots.

`SnapshotRepository` creates, activates, prunes, and retires snapshots. This
repository stores what an entity catalog snapshot holds, and checks the rule
that an identifier is active in at most one source.
"""

from itertools import batched
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.orm import Session, aliased

from standard_annotation_backend.domain.entities import EntityCatalog
from standard_annotation_backend.domain.snapshots import SnapshotKind
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    acquire_transaction_lock,
)
from standard_annotation_backend.persistence.models import (
    EntitySnapshotDetailRecord,
    ReferenceSnapshotRecord,
    StoredEntityRecord,
)

_INSERT_BATCH_SIZE = 5_000
"""Rows per batched statement, keeping each within PostgreSQL's parameter limit."""


class EntityRepository:
    """Store and read entity catalog snapshot rows in the caller's transaction.

    Args:
        session: Session owned by the calling unit of work.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(self, snapshot_id: UUID, catalog: EntityCatalog) -> None:
        """Store an entity catalog snapshot's detail and records.

        The parser has already rejected a catalog that lists an identifier on
        more than one line.

        Args:
            snapshot_id: A snapshot that `SnapshotRepository.create` inserted.
            catalog: The parsed catalog.
        """
        self.session.add(
            EntitySnapshotDetailRecord(
                snapshot_id=snapshot_id,
                source_format=catalog.source_format,
                source_metadata=catalog.source_metadata,
                record_statistics={
                    "source_record_count": len(catalog.records),
                    "active_identifier_count": len(
                        {record.entity.db_object_id for record in catalog.records}
                    ),
                    "warnings": list(catalog.warnings),
                },
            )
        )
        self.session.flush()
        rows = (
            {
                "snapshot_id": snapshot_id,
                "db_object_id": record.entity.db_object_id,
                "line_number": record.line_number,
                "entity": entity,
                "db_object_taxon_id": entity["db_object_taxon_id"],
                "db_object_type": entity["db_object_type"],
            }
            for record in catalog.records
            for entity in (record.entity.model_dump(mode="json"),)
        )
        for batch in batched(rows, _INSERT_BATCH_SIZE, strict=False):
            self.session.execute(insert(StoredEntityRecord), batch)

    def lock_activation(self) -> None:
        """Make entity catalog activations run one at a time until the transaction ends.

        `collisions` and the activation that follows it must not interleave with
        another source's, or two sources could both activate the same new
        identifier.
        """
        acquire_transaction_lock(self.session, LockNamespace.ENTITY_ACTIVATION)

    def collisions(self, snapshot_id: UUID, source_key: str) -> tuple[str, ...]:
        """Return the snapshot's identifiers that another source's active catalog lists.

        Call it after `lock_activation`, in the transaction that activates the
        snapshot.
        """
        other = aliased(StoredEntityRecord)
        return tuple(
            self.session.scalars(
                select(StoredEntityRecord.db_object_id)
                .join(other, other.db_object_id == StoredEntityRecord.db_object_id)
                .join(
                    ReferenceSnapshotRecord,
                    ReferenceSnapshotRecord.snapshot_id == other.snapshot_id,
                )
                .where(
                    StoredEntityRecord.snapshot_id == snapshot_id,
                    ReferenceSnapshotRecord.kind == SnapshotKind.ENTITY,
                    ReferenceSnapshotRecord.active.is_(True),
                    ReferenceSnapshotRecord.source_key != source_key,
                )
                .order_by(StoredEntityRecord.db_object_id)
            )
        )

    def identifiers(self, snapshot_id: UUID) -> set[str]:
        """Return the identifiers an entity catalog snapshot lists."""
        return set(
            self.session.scalars(
                select(StoredEntityRecord.db_object_id).where(
                    StoredEntityRecord.snapshot_id == snapshot_id
                )
            )
        )
