"""Store and read the detail and data rows of go-site rule data snapshots.

`SnapshotRepository` creates, activates, prunes, and retires snapshots. This
repository stores what a rules snapshot holds: five go-site files as validated
`RuleData` JSON, and the PMIDs that `retracted-publications.txt` lists.
"""

from collections.abc import Collection
from itertools import batched
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.semantic_validation.rule_data import (
    STORED_RULE_DATA,
    RuleData,
)
from standard_annotation_backend.persistence.models import (
    RetractedPublicationRecord,
    RuleDataRecord,
    RulesSnapshotDetailRecord,
)
from standard_annotation_backend.persistence.repositories.snapshots import (
    SnapshotPrunedError,
)

_INSERT_BATCH_SIZE = 5_000


class RulesRepository:
    """Store and read rules snapshot rows in the caller's transaction.

    Args:
        session: Session owned by the calling unit of work.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(
        self,
        snapshot_id: UUID,
        rule_data: dict[str, object],
        retracted_pmids: Collection[str],
    ) -> None:
        """Store a rules snapshot's rule data and retracted PMIDs.

        Args:
            snapshot_id: A snapshot that `SnapshotRepository.create` inserted.
            rule_data: The parsed files in the JSON form that `STORED_RULE_DATA`
                produces.
            retracted_pmids: Every PMID that `retracted-publications.txt` lists.
        """
        self.session.add_all(
            [
                RulesSnapshotDetailRecord(
                    snapshot_id=snapshot_id,
                    retracted_publication_count=len(retracted_pmids),
                ),
                RuleDataRecord(snapshot_id=snapshot_id, data=rule_data),
            ]
        )
        self.session.flush()
        rows = (
            {"snapshot_id": snapshot_id, "pmid": pmid}
            for pmid in sorted(retracted_pmids)
        )
        for batch in batched(rows, _INSERT_BATCH_SIZE, strict=False):
            self.session.execute(insert(RetractedPublicationRecord), batch)

    def rule_data(self, snapshot_id: UUID) -> RuleData:
        """Return a rules snapshot's decoded rule data.

        Raises:
            SnapshotPrunedError: If the snapshot's rule data was pruned.
            StoredDataError: If the stored rule data cannot be decoded.
        """
        stored = self.session.get(RuleDataRecord, snapshot_id)
        if stored is None:
            raise SnapshotPrunedError((snapshot_id,))
        return STORED_RULE_DATA.load(stored.data)

    def retracted_publication_count(self, snapshot_id: UUID) -> int:
        """Return how many PMIDs a rules snapshot lists as retracted.

        Detail rows are never pruned, so the count stays available after the
        snapshot's data is deleted.

        Raises:
            SnapshotPrunedError: If no rules snapshot has the ID, so it has no
                detail row.
        """
        detail = self.session.get(RulesSnapshotDetailRecord, snapshot_id)
        if detail is None:
            raise SnapshotPrunedError((snapshot_id,))
        return detail.retracted_publication_count
