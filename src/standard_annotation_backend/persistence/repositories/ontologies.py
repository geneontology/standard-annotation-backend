"""Store and read the detail and data rows of ontology snapshots.

`SnapshotRepository` creates, activates, prunes, and retires snapshots. This
repository stores what an ontology snapshot holds: how its document was read,
its terms, its alternative identifiers, and its closure rows.
"""

from collections.abc import Iterable
from itertools import batched
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.ontology import (
    OntologyClosureRow,
    OntologySnapshot,
)
from standard_annotation_backend.persistence.models import (
    OntologyAltIdRecord,
    OntologyClosureRecord,
    OntologySnapshotDetailRecord,
    OntologyTermRecord,
)

_INSERT_BATCH_SIZE = 5_000


class OntologyRepository:
    """Store and read ontology snapshot rows in the caller's transaction.

    Args:
        session: Session owned by the calling unit of work.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(
        self,
        snapshot_id: UUID,
        snapshot: OntologySnapshot,
        closure_rows: Iterable[OntologyClosureRow],
    ) -> int:
        """Store an ontology snapshot's detail, terms, alternative IDs, and closure.

        Args:
            snapshot_id: A snapshot that `SnapshotRepository.create` inserted.
            snapshot: The parsed ontology.
            closure_rows: Its closure, as `compute_closure` yields it.

        Returns:
            The number of closure rows stored.
        """
        self.session.add(
            OntologySnapshotDetailRecord(
                snapshot_id=snapshot_id,
                document_version=snapshot.document_version,
                loaded_predicates=list(snapshot.closure_predicates),
                identifier_prefixes=list(snapshot.identifier_prefixes),
            )
        )
        self.session.flush()
        term_rows = (
            {
                "snapshot_id": snapshot_id,
                "term_id": term.term_id,
                "obsolete": term.obsolete,
                "replaced_by": list(term.replaced_by),
                "consider": list(term.consider),
                "subsets": list(term.subsets),
            }
            for term in sorted(snapshot.terms.values(), key=lambda value: value.term_id)
        )
        for batch in batched(term_rows, _INSERT_BATCH_SIZE, strict=False):
            self.session.execute(insert(OntologyTermRecord), batch)
        alt_id_rows = (
            {"snapshot_id": snapshot_id, "alt_id": alt_id, "term_id": term_id}
            for alt_id, term_id in sorted(snapshot.alt_ids.items())
        )
        for batch in batched(alt_id_rows, _INSERT_BATCH_SIZE, strict=False):
            self.session.execute(insert(OntologyAltIdRecord), batch)
        closure_count = 0
        closure_values = (
            {
                "snapshot_id": snapshot_id,
                "subject_term_id": row.subject_term_id,
                "predicate_id": row.predicate_id,
                "object_term_id": row.object_term_id,
                "depth": row.depth,
            }
            for row in closure_rows
        )
        for batch in batched(closure_values, _INSERT_BATCH_SIZE, strict=False):
            self.session.execute(insert(OntologyClosureRecord), batch)
            closure_count += len(batch)
        self.session.flush()
        return closure_count

    def detail(self, snapshot_id: UUID) -> OntologySnapshotDetailRecord | None:
        """Return how an ontology snapshot's document was read."""
        return self.session.get(OntologySnapshotDetailRecord, snapshot_id)
