"""Store and read the detail and data rows of entity catalog snapshots.

`SnapshotRepository` creates, activates, prunes, and retires snapshots. This
repository stores what an entity catalog snapshot holds, and enforces the rule
that an identifier is active in at most one source.
"""

from itertools import batched
from uuid import UUID

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session, aliased

from standard_annotation_backend.domain.entities import (
    EntityCatalog,
    EntityCollision,
    EntitySnapshotDetail,
)
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

        Each identifier must appear in the catalog at most once, as
        `reject_repeated_identifiers` ensures.

        Args:
            snapshot_id: A snapshot that `SnapshotRepository.create` inserted.
            catalog: The valid entities read from the file.
        """
        self.session.add(
            EntitySnapshotDetailRecord(
                snapshot_id=snapshot_id,
                source_format=catalog.source_format,
                source_metadata=catalog.source_metadata,
                schema_version=catalog.schema_version,
                rejected_collisions=False,
            )
        )
        self.session.flush()
        rows = (
            {
                "snapshot_id": snapshot_id,
                "db_object_id": record.item.db_object_id,
                "line_number": record.line_number,
                "entity": entity,
                "db_object_taxon_id": entity["db_object_taxon_id"],
                "db_object_type": entity["db_object_type"],
            }
            for record in catalog.records
            for entity in (record.item.model_dump(mode="json"),)
        )
        for batch in batched(rows, _INSERT_BATCH_SIZE, strict=False):
            self.session.execute(insert(StoredEntityRecord), batch)

    def lock_activation(self) -> None:
        """Make entity catalog activations run one at a time until the transaction ends.

        `reject_collisions` and the activation that follows it must not interleave with
        another source's, or two sources could both activate the same new
        identifier.
        """
        acquire_transaction_lock(self.session, LockNamespace.ENTITY_ACTIVATION)

    def reject_collisions(
        self, snapshot_id: UUID, source_key: str
    ) -> tuple[EntityCollision, ...]:
        """Delete the snapshot's entities that another source's active catalog lists.

        An identifier is active in at most one source, and the source that
        activated it first keeps it. Call this after `lock_activation`, in the
        transaction that activates the snapshot, so that no other source
        activates one of the snapshot's identifiers in between.

        When it deletes at least one entity, it also records on the snapshot's
        detail that it rejected collisions; see `snapshot_detail`.

        Returns:
            One collision for each deleted entity, in line order.
        """
        other = aliased(StoredEntityRecord)
        rows = self.session.execute(
            delete(StoredEntityRecord)
            .where(
                StoredEntityRecord.snapshot_id == snapshot_id,
                StoredEntityRecord.db_object_id == other.db_object_id,
                other.snapshot_id == ReferenceSnapshotRecord.snapshot_id,
                ReferenceSnapshotRecord.kind == SnapshotKind.ENTITY,
                ReferenceSnapshotRecord.active.is_(True),
                ReferenceSnapshotRecord.source_key != source_key,
            )
            .returning(
                StoredEntityRecord.db_object_id,
                StoredEntityRecord.line_number,
                ReferenceSnapshotRecord.source_key,
            )
            .execution_options(synchronize_session=False)
        )
        collisions = tuple(
            sorted(
                (
                    EntityCollision(db_object_id, line_number, other_source)
                    for db_object_id, line_number, other_source in rows
                ),
                key=lambda collision: collision.line_number,
            )
        )
        if collisions:
            self.session.execute(
                update(EntitySnapshotDetailRecord)
                .where(EntitySnapshotDetailRecord.snapshot_id == snapshot_id)
                .values(rejected_collisions=True)
                .execution_options(synchronize_session=False)
            )
        return collisions

    def snapshot_detail(self, snapshot_id: UUID) -> EntitySnapshotDetail | None:
        """Return how the snapshot's file was read and activated.

        Returns:
            The schema version that checked the rows and whether activation
            rejected any as a collision, or `None` if the snapshot has no detail
            row.
        """
        row = self.session.execute(
            select(
                EntitySnapshotDetailRecord.schema_version,
                EntitySnapshotDetailRecord.rejected_collisions,
            ).where(EntitySnapshotDetailRecord.snapshot_id == snapshot_id)
        ).one_or_none()
        if row is None:
            return None
        return EntitySnapshotDetail(
            schema_version=row.schema_version,
            rejected_collisions=row.rejected_collisions,
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
