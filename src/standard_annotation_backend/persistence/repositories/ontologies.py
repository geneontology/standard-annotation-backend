"""Persist immutable ontology snapshots in caller-managed transactions."""

from collections.abc import Iterable, Iterator
from datetime import datetime
from itertools import islice
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.ontology import (
    OntologyCandidateConflictError,
    OntologyClosureRow,
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
)
from standard_annotation_backend.persistence.models import (
    JobRecord,
    OntologyClosureRecord,
    OntologyMetadataRecord,
    OntologyTermRecord,
)

_INSERT_BATCH_SIZE = 5_000


class OntologyVersionNotFoundError(LookupError):
    """Report an operation that targets an unknown ontology snapshot."""


class OntologySnapshotPrunedError(RuntimeError):
    """Report that required ontology term or closure rows were deleted."""


class OntologyRepository:
    """Store and select versioned ontology data within one transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def stage(
        self,
        *,
        job_id: UUID,
        document: OntologyDocument,
        snapshot: OntologySnapshot,
        closure_rows: Iterable[OntologyClosureRow],
    ) -> OntologyMetadataRecord:
        """Persist a complete inactive snapshot and flush all of its rows.

        Each job stages at most one snapshot. If the job already staged a snapshot
        from the same source, that existing snapshot is returned unchanged, so a
        retried job resumes its own candidate instead of staging another one.

        Returns:
            The job's staged snapshot metadata.

        Raises:
            ValueError: If `snapshot` was not parsed from `document`.
            OntologyCandidateConflictError: If the job already staged a snapshot
                from a different source type, locator, revision, or checksum.
        """
        if snapshot.document != document:
            raise ValueError("snapshot document does not match staged document")
        existing = self.get_by_job(job_id)
        if existing is not None:
            if (
                existing.ontology_key,
                existing.source_type,
                existing.source_locator,
                existing.source_revision,
                existing.source_checksum,
            ) != (
                document.ontology_key.value,
                document.source_type,
                document.source_locator,
                document.source_revision,
                document.source_checksum,
            ):
                raise OntologyCandidateConflictError(
                    "staged ontology source differs from the resolved document"
                )
            return existing
        record = OntologyMetadataRecord(
            ontology_key=document.ontology_key.value,
            source_type=document.source_type,
            source_locator=document.source_locator,
            source_revision=document.source_revision,
            source_checksum=document.source_checksum,
            document_version=snapshot.document_version,
            loaded_predicates=list(snapshot.closure_predicates),
            job_id=job_id,
            fetched_at=document.fetched_at,
            active=False,
        )
        self.session.add(record)
        self.session.flush([record])

        term_rows = (
            {
                "version_id": record.version_id,
                "term_id": term.term_id,
                "obsolete": term.obsolete,
                "replaced_by": list(term.replaced_by),
                "consider": list(term.consider),
            }
            for term in sorted(snapshot.terms.values(), key=lambda value: value.term_id)
        )
        for batch in _batches(term_rows):
            self.session.execute(insert(OntologyTermRecord), batch)

        closure_values = (
            {
                "version_id": record.version_id,
                "subject_term_id": row.subject_term_id,
                "predicate_id": row.predicate_id,
                "object_term_id": row.object_term_id,
                "depth": row.depth,
            }
            for row in closure_rows
        )
        for batch in _batches(closure_values):
            self.session.execute(insert(OntologyClosureRecord), batch)
        self.session.flush()
        return record

    def get_by_job(self, job_id: UUID) -> OntologyMetadataRecord | None:
        """Return the snapshot created by a job, if one exists."""
        return self.session.scalar(
            select(OntologyMetadataRecord).where(
                OntologyMetadataRecord.job_id == job_id
            )
        )

    def lock_activation_state(
        self, job_id: UUID
    ) -> tuple[OntologyMetadataRecord, OntologyMetadataRecord | None]:
        """Lock and return a job's candidate and the active snapshot for its key.

        The active snapshot is the candidate itself when the candidate was already
        activated. Snapshots are ordered by when they were staged, so a candidate
        staged before the active snapshot can never replace it.

        Raises:
            OntologyVersionNotFoundError: If the job has not staged a snapshot.
            OntologyCandidateConflictError: If the candidate is inactive and a
                snapshot staged after it is already active.
        """
        candidate = self.session.scalar(
            select(OntologyMetadataRecord)
            .where(OntologyMetadataRecord.job_id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if candidate is None:
            raise OntologyVersionNotFoundError(job_id)
        records = tuple(
            self.session.scalars(
                select(OntologyMetadataRecord)
                .where(
                    OntologyMetadataRecord.ontology_key == candidate.ontology_key,
                    OntologyMetadataRecord.active.is_(True),
                )
                .order_by(OntologyMetadataRecord.version_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        active = next(
            (row for row in records if row.version_id != candidate.version_id), None
        )
        if candidate.active:
            active = candidate
        elif (
            active is not None and active.staging_sequence > candidate.staging_sequence
        ):
            raise OntologyCandidateConflictError(
                "a newer ontology snapshot is already active"
            )
        return candidate, active

    def get_active(self, key: OntologyKey) -> OntologyMetadataRecord | None:
        """Return the active snapshot for an ontology key."""
        return self.session.scalar(
            select(OntologyMetadataRecord).where(
                OntologyMetadataRecord.ontology_key == key.value,
                OntologyMetadataRecord.active.is_(True),
            )
        )

    def matches_active_source(self, document: OntologyDocument) -> bool:
        """Return whether source details and checksum match the active snapshot."""
        active = self.get_active(document.ontology_key)
        return active is not None and (
            active.source_type,
            active.source_locator,
            active.source_revision,
            active.source_checksum,
        ) == (
            document.source_type,
            document.source_locator,
            document.source_revision,
            document.source_checksum,
        )

    def activate(self, version_id: UUID) -> OntologyMetadataRecord:
        """Make one snapshot active and deactivate the prior snapshot for its key."""
        record = self._require_complete(version_id, lock=True)
        self.session.execute(
            update(OntologyMetadataRecord)
            .where(
                OntologyMetadataRecord.ontology_key == record.ontology_key,
                OntologyMetadataRecord.active.is_(True),
            )
            .values(active=False)
        )
        self.session.flush()
        record.active = True
        self.session.flush([record])
        return record

    def record_refresh_result(
        self, record: OntologyMetadataRecord, result: dict[str, object]
    ) -> None:
        """Store a successful activation result in the current transaction."""
        record.refresh_result = result
        self.session.flush([record])

    def prune_candidates(
        self, key: OntologyKey, pruned_at: datetime
    ) -> tuple[UUID, ...]:
        """Delete term and closure rows for snapshots outside the retention set.

        The active snapshot, newest successfully activated predecessor, and snapshots
        for queued or running jobs remain complete. Metadata rows are locked until
        the caller ends the transaction.

        Args:
            key: Ontology whose stored snapshots should be pruned.
            pruned_at: Time to record on each snapshot that is pruned.

        Returns:
            Version identifiers for snapshots pruned by this call.
        """
        rows = tuple(
            self.session.execute(
                select(OntologyMetadataRecord, JobRecord.status)
                .join(JobRecord, JobRecord.job_id == OntologyMetadataRecord.job_id)
                .where(
                    OntologyMetadataRecord.ontology_key == key.value,
                    OntologyMetadataRecord.bulk_data_pruned_at.is_(None),
                )
                .order_by(OntologyMetadataRecord.staging_sequence)
                .with_for_update(of=OntologyMetadataRecord)
                .execution_options(populate_existing=True)
            )
        )
        predecessor = next(
            (
                record
                for record, _ in reversed(rows)
                if not record.active and record.refresh_result is not None
            ),
            None,
        )
        candidates = tuple(
            record
            for record, status in rows
            if not record.active
            and record is not predecessor
            and status not in {"queued", "running"}
        )
        version_ids = tuple(record.version_id for record in candidates)
        if not version_ids:
            return ()
        # This intentionally physically deletes immutable snapshot data to bound
        # storage. Standard annotations are managed records and use versioning and
        # soft deletion instead. Snapshot metadata, job results, and audit events
        # remain as provenance.
        self.session.execute(
            delete(OntologyClosureRecord).where(
                OntologyClosureRecord.version_id.in_(version_ids)
            )
        )
        self.session.execute(
            delete(OntologyTermRecord).where(
                OntologyTermRecord.version_id.in_(version_ids)
            )
        )
        for record in candidates:
            record.bulk_data_pruned_at = pruned_at
        self.session.flush(candidates)
        return version_ids

    def closure_supported(self, key: OntologyKey, predicate_id: str) -> bool:
        """Return whether the active snapshot loaded closure for a predicate."""
        active = self.get_active(key)
        return active is not None and predicate_id in active.loaded_predicates

    def list_terms(self, version_id: UUID) -> list[OntologyTermRecord]:
        """Return snapshot terms in stable identifier order."""
        self._require_complete(version_id)
        return list(
            self.session.scalars(
                select(OntologyTermRecord)
                .where(OntologyTermRecord.version_id == version_id)
                .order_by(OntologyTermRecord.term_id)
            )
        )

    def list_closure(self, version_id: UUID) -> list[OntologyClosureRecord]:
        """Return snapshot closure in stable subject, predicate, and object order."""
        self._require_complete(version_id)
        return list(
            self.session.scalars(
                select(OntologyClosureRecord)
                .where(OntologyClosureRecord.version_id == version_id)
                .order_by(
                    OntologyClosureRecord.subject_term_id,
                    OntologyClosureRecord.predicate_id,
                    OntologyClosureRecord.object_term_id,
                )
            )
        )

    def term_count(self, version_id: UUID) -> int:
        """Return the number of terms in one snapshot without loading them."""
        self._require_complete(version_id)
        return (
            self.session.scalar(
                select(func.count())
                .select_from(OntologyTermRecord)
                .where(OntologyTermRecord.version_id == version_id)
            )
            or 0
        )

    def closure_count(self, version_id: UUID) -> int:
        """Return the number of closure rows without loading them."""
        self._require_complete(version_id)
        return (
            self.session.scalar(
                select(func.count())
                .select_from(OntologyClosureRecord)
                .where(OntologyClosureRecord.version_id == version_id)
            )
            or 0
        )

    def _require_complete(
        self, version_id: UUID, *, lock: bool = False
    ) -> OntologyMetadataRecord:
        """Lock and return a snapshot whose term and closure rows remain.

        Reads use a shared row lock so pruning cannot delete data before the caller
        finishes its transaction. Activation requests an exclusive lock. The query
        refreshes metadata already loaded by this session so pruning committed by
        another transaction is visible.

        Args:
            version_id: Snapshot version to check.
            lock: Whether to use an exclusive lock for activation.

        Returns:
            Current metadata for the complete snapshot.

        Raises:
            OntologyVersionNotFoundError: If the snapshot does not exist.
            OntologySnapshotPrunedError: If its term and closure rows were deleted.
        """
        statement = select(OntologyMetadataRecord).where(
            OntologyMetadataRecord.version_id == version_id
        )
        if lock:
            statement = statement.with_for_update()
        else:
            statement = statement.with_for_update(read=True)
        record = self.session.scalar(
            statement.execution_options(populate_existing=True)
        )
        if record is None:
            raise OntologyVersionNotFoundError(version_id)
        if record.bulk_data_pruned_at is not None:
            raise OntologySnapshotPrunedError(version_id)
        return record


def _batches[T](rows: Iterable[T]) -> Iterator[list[T]]:
    """Yield lists of at most 5,000 rows without loading every row at once."""
    iterator = iter(rows)
    while batch := list(islice(iterator, _INSERT_BATCH_SIZE)):
        yield batch
