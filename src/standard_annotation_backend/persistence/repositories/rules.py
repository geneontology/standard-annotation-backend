"""Store and activate snapshots of the go-site metadata that GO Rules read.

A snapshot is the parsed content of the go-site files from one `rules` refresh.
GO Rule checks read the one active snapshot.
"""

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
        """Return the snapshot a job stored, unless a later activation deleted it."""
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
        that release store the files again instead of keeping the values an earlier
        release stored.

        Args:
            record: The snapshot to compare.
            rule_data: The parsed files in the JSON form that `STORED_RULE_DATA`
                produces.
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

        Activations run one at a time. Each takes a PostgreSQL advisory lock (a
        lock on an application-chosen key rather than on a row) that is held until
        its transaction commits or rolls back, and a second activation waits for
        it. Under that lock, if the active snapshot came from the same files and
        already `stores` this rule data and these PMIDs, nothing changes and that
        snapshot is returned with `applied=False`. Otherwise the new snapshot and its
        retracted publications are inserted and activated. The snapshot it replaces
        is kept, so a validation that looked up that snapshot just before can still
        read it. Every older snapshot is deleted, with its retracted publications.

        The caller commits, and records any audit event.

        Args:
            job_id: Job that fetched the files.
            provenance: Where the files came from, at which commit, and the
                checksum of their combined content.
            rule_data: The parsed files in the JSON form that `STORED_RULE_DATA`
                produces.
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
