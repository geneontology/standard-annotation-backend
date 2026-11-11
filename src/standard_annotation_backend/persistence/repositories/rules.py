"""Store and activate snapshots of the go-site metadata that GO Rules read."""

from collections.abc import Collection
from dataclasses import dataclass
from itertools import batched
from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.refresh import SourceProvenance
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    acquire_transaction_lock,
)
from standard_annotation_backend.persistence.models import (
    RetractedPublicationRecord,
    RulesSnapshotRecord,
)

_INSERT_BATCH_SIZE = 5_000


def same_rule_files(record: RulesSnapshotRecord, provenance: SourceProvenance) -> bool:
    """Return whether a snapshot was built from the same files as `provenance`.

    The commit is not compared. Most go-site commits change none of these files,
    and comparing the commit would store an identical snapshot after each one.
    """
    return (record.source_type, record.source_locator, record.source_checksum) == (
        provenance.source_type,
        provenance.source_locator,
        provenance.source_checksum,
    )


@dataclass(frozen=True, slots=True)
class RulesActivation:
    """Describe the active snapshot after `RulesRepository.activate`.

    Attributes:
        record: The active snapshot.
        applied: Whether this call stored it. `False` means the active snapshot
            already came from the same files.
    """

    record: RulesSnapshotRecord
    applied: bool


class RulesRepository:
    """Read and activate GO Rule data snapshots in the caller's transaction.

    Args:
        session: Session owned by the calling unit of work.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_active(self) -> RulesSnapshotRecord | None:
        """Return the active snapshot, if there is one."""
        return self.session.scalar(
            select(RulesSnapshotRecord).where(RulesSnapshotRecord.active.is_(True))
        )

    def get_by_job(self, job_id: UUID) -> RulesSnapshotRecord | None:
        """Return the snapshot a job stored, if retention has kept it."""
        return self.session.scalar(
            select(RulesSnapshotRecord).where(RulesSnapshotRecord.job_id == job_id)
        )

    def stores(
        self,
        record: RulesSnapshotRecord,
        rule_data: dict[str, object],
        retracted_pmids: Collection[str],
    ) -> bool:
        """Return whether a snapshot holds exactly this rule data and these PMIDs.

        A SABE release that parses or stores the files differently gets different
        values from the same files. Comparing the values, not only the files, lets
        that release store the files again instead of keeping the old form.

        Args:
            record: The snapshot to compare.
            rule_data: The `STORED_RULE_DATA` form of the parsed files.
            retracted_pmids: Every PMID that `retracted-publications.txt` lists.
        """
        if record.rule_data != rule_data or record.retracted_publication_count != len(
            retracted_pmids
        ):
            return False
        stored = self.session.scalars(
            select(RetractedPublicationRecord.pmid).where(
                RetractedPublicationRecord.snapshot_id == record.snapshot_id
            )
        )
        return set(stored) == set(retracted_pmids)

    def activate(
        self,
        *,
        job_id: UUID,
        provenance: SourceProvenance,
        rule_data: dict[str, object],
        retracted_pmids: Collection[str],
    ) -> RulesActivation:
        """Store a snapshot and make it the active one.

        A transaction-scoped advisory lock lets one activation run at a time; a
        second waits until the first commits or rolls back. Under that lock, if the
        active snapshot came from the same files and already `stores` this rule
        data and these PMIDs, nothing changes and that snapshot is returned with
        `applied=False`. Otherwise the new snapshot and
        its retracted publications are inserted and activated. The snapshot it
        replaces is kept, so a reader that resolved it just before still finds its
        data. Every older snapshot is deleted, with its retracted publications.

        The caller owns the commit, including any audit event.

        Args:
            job_id: Job that fetched the files.
            provenance: Identifies the packed files.
            rule_data: The `STORED_RULE_DATA` form of the parsed files.
            retracted_pmids: Every PMID that `retracted-publications.txt` lists.

        Returns:
            The active snapshot, and whether this call stored it.
        """
        # Hold the lock from the duplicate check through activation, so a
        # concurrent activation of the same files waits and then finds this one.
        acquire_transaction_lock(self.session, LockNamespace.RULES_REFRESH)
        previous = self.get_active()
        if (
            previous is not None
            and same_rule_files(previous, provenance)
            and self.stores(previous, rule_data, retracted_pmids)
        ):
            return RulesActivation(previous, applied=False)
        record = RulesSnapshotRecord(
            job_id=job_id,
            source_type=provenance.source_type,
            source_locator=provenance.source_locator,
            source_revision=provenance.source_revision,
            source_checksum=provenance.source_checksum,
            fetched_at=provenance.fetched_at,
            active=False,
            rule_data=rule_data,
            retracted_publication_count=len(retracted_pmids),
        )
        self.session.add(record)
        self.session.flush([record])
        rows = (
            {"snapshot_id": record.snapshot_id, "pmid": pmid}
            for pmid in sorted(retracted_pmids)
        )
        for batch in batched(rows, _INSERT_BATCH_SIZE, strict=False):
            self.session.execute(insert(RetractedPublicationRecord), batch)
        kept = [record.snapshot_id]
        # Deactivate first: the partial unique index allows one active snapshot.
        if previous is not None:
            previous.active = False
            self.session.flush([previous])
            kept.append(previous.snapshot_id)
        record.active = True
        self.session.flush([record])
        self.session.execute(
            delete(RulesSnapshotRecord).where(
                RulesSnapshotRecord.snapshot_id.not_in(kept)
            )
        )
        return RulesActivation(record, applied=True)
