"""Persist immutable ontology snapshots in caller-managed transactions."""

from collections.abc import Iterable, Iterator
from itertools import islice
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.ontology import (
    OntologyClosureRow,
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
)
from standard_annotation_backend.persistence.models import (
    OntologyClosureRecord,
    OntologyMetadataRecord,
    OntologyTermRecord,
)

_INSERT_BATCH_SIZE = 5_000


class OntologyVersionNotFoundError(LookupError):
    """Report an operation that targets an unknown ontology snapshot."""


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
        """Persist a complete inactive snapshot and flush all of its rows."""
        if snapshot.document != document:
            raise ValueError("snapshot document does not match staged document")
        record = OntologyMetadataRecord(
            ontology_key=document.ontology_key.value,
            source_type=document.source_type,
            source_locator=document.source_locator,
            source_revision=document.source_revision,
            source_checksum=document.source_checksum,
            document_version=snapshot.document_version,
            loaded_predicates=list(snapshot.closure_predicates),
            job_id=job_id,
            loaded_at=document.retrieved_at,
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
        """Lock and return a job's candidate and the active snapshot for its key."""
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
        record = self.session.scalar(
            select(OntologyMetadataRecord)
            .where(OntologyMetadataRecord.version_id == version_id)
            .with_for_update()
        )
        if record is None:
            raise OntologyVersionNotFoundError(version_id)
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

    def record_load_result(
        self, record: OntologyMetadataRecord, result: dict[str, object]
    ) -> None:
        """Store a successful activation result in the current transaction."""
        record.load_result = result
        self.session.flush([record])

    def closure_supported(self, key: OntologyKey, predicate_id: str) -> bool:
        """Return whether the active snapshot loaded closure for a predicate."""
        active = self.get_active(key)
        return active is not None and predicate_id in active.loaded_predicates

    def list_terms(self, version_id: UUID) -> list[OntologyTermRecord]:
        """Return snapshot terms in stable identifier order."""
        return list(
            self.session.scalars(
                select(OntologyTermRecord)
                .where(OntologyTermRecord.version_id == version_id)
                .order_by(OntologyTermRecord.term_id)
            )
        )

    def list_closure(self, version_id: UUID) -> list[OntologyClosureRecord]:
        """Return snapshot closure in stable subject, predicate, and object order."""
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
        return (
            self.session.scalar(
                select(func.count())
                .select_from(OntologyClosureRecord)
                .where(OntologyClosureRecord.version_id == version_id)
            )
            or 0
        )


def _batches[T](rows: Iterable[T]) -> Iterator[list[T]]:
    """Yield lists of at most 5,000 rows without loading every row at once."""
    iterator = iter(rows)
    while batch := list(islice(iterator, _INSERT_BATCH_SIZE)):
        yield batch
