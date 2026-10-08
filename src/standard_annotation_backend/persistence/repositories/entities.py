"""Store, publish, and retire entity catalogs in the caller's transaction."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import batched
from uuid import UUID

from psycopg.errors import UniqueViolation
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotations import (
    AnnotationStatus,
)
from standard_annotation_backend.domain.entities import (
    STORED_ENTITY_REFRESH_RESULT,
    STORED_ENTITY_RETIREMENT_RESULT,
    EntityCandidateConflictError,
    EntityCatalog,
    EntityCatalogCollisionError,
    EntityCatalogRetirementResult,
    EntityRefreshResult,
    EntityRemovalImpact,
    UnknownDbObjectIdError,
)
from standard_annotation_backend.domain.jobs import (
    ACTIVE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
)
from standard_annotation_backend.persistence.locks import (
    LockNamespace,
    acquire_transaction_lock,
)
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    EntityCatalogSnapshotRecord,
    EntityMembershipRecord,
    EntitySourceRecord,
    EntityStagingRecord,
    JobRecord,
)
from standard_annotation_backend.persistence.repositories.jobs import lock_job_record

_INSERT_BATCH_SIZE = 5_000
"""Rows per batched statement, keeping each within PostgreSQL's parameter limit."""
_ENTITY_JOB_TYPES = frozenset({"entity_refresh", "entity_retirement"})


@dataclass(frozen=True, slots=True)
class _VerifiedStaging:
    """Staged rows that match the candidate's stored counts and checksum.

    Attributes:
        rows: Staged rows in line-number order, as inserted into source records.
        db_object_ids: Distinct identifiers in the staging.
        warnings: The catalog warnings stored when the staging was written.
    """

    rows: list[dict[str, object]]
    db_object_ids: frozenset[str]
    warnings: tuple[str, ...]


class EntityRepository:
    """Read and change the entity catalog tables in the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def active_source_keys(self) -> tuple[str, ...]:
        """Return source keys that currently have an active catalog."""
        return tuple(
            self.session.scalars(
                select(EntityCatalogSnapshotRecord.source_key)
                .where(EntityCatalogSnapshotRecord.active.is_(True))
                .order_by(EntityCatalogSnapshotRecord.source_key)
            )
        )

    def lock_job(self, job_id: UUID) -> JobRecord:
        """Lock a job's row until the transaction ends and return the job.

        Staging, publication, retirement, and cleanup for one job therefore run
        one at a time.

        Raises:
            EntityCandidateConflictError: If the job does not exist or is not an
                entity job.
        """
        job = lock_job_record(self.session, job_id)
        if job is None or job.job_type not in _ENTITY_JOB_TYPES:
            raise EntityCandidateConflictError
        return job

    def retire(
        self, job_id: UUID, source_key: str, *, configured: bool
    ) -> tuple[EntityCatalogRetirementResult, bool]:
        """Deactivate a source's catalog and delete its membership rows.

        The source's catalog lock is held, so no publication for the source runs
        at the same time. Membership rows are locked before affected annotations
        are listed, so an annotation being saved with a removed identifier is
        included in the result. If this job already retired the catalog, the
        stored result is returned and nothing changes.

        Args:
            job_id: The retirement job.
            source_key: The source whose catalog is retired.
            configured: Whether the source is currently configured. A configured
                source is never retired.

        Returns:
            The result and whether this call changed any state.
        """
        self.lock_job(job_id)
        acquire_transaction_lock(self.session, LockNamespace.ENTITY_CATALOG, source_key)
        recorded = self.session.scalar(
            select(EntityCatalogSnapshotRecord).where(
                EntityCatalogSnapshotRecord.retired_by_job_id == job_id
            )
        )
        if recorded is not None and recorded.retirement_result is not None:
            return (
                STORED_ENTITY_RETIREMENT_RESULT.load(recorded.retirement_result),
                False,
            )
        not_retired = EntityCatalogRetirementResult(source_key, False, None, ())
        if configured:
            return not_retired, False
        current = self.session.scalar(
            select(EntityCatalogSnapshotRecord)
            .where(
                EntityCatalogSnapshotRecord.source_key == source_key,
                EntityCatalogSnapshotRecord.active.is_(True),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current is None:
            return not_retired, False
        removed = set(
            self.session.scalars(
                select(EntityMembershipRecord.db_object_id)
                .where(EntityMembershipRecord.source_key == source_key)
                .order_by(EntityMembershipRecord.db_object_id)
                .with_for_update()
            )
        )
        impacts = self._removal_impacts(removed)
        self.session.execute(
            delete(EntityMembershipRecord).where(
                EntityMembershipRecord.source_key == source_key
            )
        )
        result = EntityCatalogRetirementResult(
            source_key=source_key,
            retired=True,
            snapshot_id=current.snapshot_id,
            removal_impacts=impacts,
        )
        current.active = False
        current.retired_at = datetime.now(UTC)
        current.retired_by_job_id = job_id
        current.retirement_result = result.to_job_result()
        self.session.flush([current])
        return result, True

    def stage(
        self,
        job_id: UUID,
        source_key: str,
        catalog: EntityCatalog,
    ) -> EntityCatalogSnapshotRecord:
        """Store a parsed catalog as an inactive snapshot with staging rows.

        Annotation validation ignores staging until the catalog is published.
        Each job stages at most one catalog. If the job has already staged one
        (for example, when its task runs again), the source, URL, checksum, and
        catalog checksum (`catalog_sha256`) must match the new parse; the fetch
        time may differ. Until the catalog is published, its staged rows are
        also checked again. The catalog checksum covers the format, metadata,
        warnings, and every row, and stays on the snapshot after publication.

        Raises:
            EntityCandidateConflictError: If the job's existing staging differs
                from this catalog or is incomplete or altered, or the job has
                already finished.
        """
        job = self.lock_job(job_id)
        rows = [
            {
                "job_id": job_id,
                "line_number": record.line_number,
                "db_object_id": record.entity.db_object_id,
                "entity": record.entity.model_dump(mode="json"),
            }
            for record in catalog.records
        ]
        fingerprint = _catalog_digest(
            catalog.source_format, catalog.source_metadata, rows, catalog.warnings
        )
        statistics: dict[str, object] = {"catalog_sha256": fingerprint}
        existing = self._candidate(job_id)
        if existing is not None:
            if (
                existing.source_key != source_key
                or existing.source_locator != catalog.source.source_locator
                or existing.source_revision != catalog.source.source_revision
                or existing.source_checksum != catalog.source.source_checksum
                or existing.source_statistics != statistics
            ):
                raise EntityCandidateConflictError
            if existing.publication_result is None:
                self._verify_staging(existing)
            return existing
        if job.status not in ACTIVE_JOB_STATUSES:
            raise EntityCandidateConflictError
        candidate = EntityCatalogSnapshotRecord(
            job_id=job_id,
            source_key=source_key,
            source_type=catalog.source.source_type,
            source_locator=catalog.source.source_locator,
            source_revision=catalog.source.source_revision,
            source_checksum=catalog.source.source_checksum,
            source_format=catalog.source_format,
            source_metadata=catalog.source_metadata,
            source_statistics=statistics,
            record_statistics={
                "source_record_count": len(rows),
                "active_identifier_count": len({row["db_object_id"] for row in rows}),
                "warnings": list(catalog.warnings),
            },
            fetched_at=catalog.source.fetched_at,
            staged_at=datetime.now(UTC),
            active=False,
        )
        self.session.add(candidate)
        self.session.flush([candidate])
        for batch in batched(rows, _INSERT_BATCH_SIZE, strict=False):
            self.session.execute(insert(EntityStagingRecord), batch)
        return candidate

    def publish(self, job_id: UUID) -> EntityRefreshResult:
        """Replace a source's active catalog with a job's staged catalog.

        The source's catalog lock is held until the transaction ends, so only one
        publication or retirement for a source runs at a time. Fetching, parsing,
        staging, and work for other sources are not blocked. If this job already
        published its catalog, the stored result is returned unchanged.

        The method checks the staged rows and rejects identifiers that another
        source already supplies. It then deactivates the previous snapshot,
        replaces the source's membership and source record rows, activates the
        new snapshot, stores the result on it, and deletes the job's staging.
        The previous snapshot keeps its details but loses its membership and
        source record rows. The caller records the audit event and commits; any
        failure rolls back and leaves the previous catalog active.

        Membership rows are locked before affected annotations are listed, so an
        annotation being saved with a removed identifier is included in
        `removal_impacts`. Removing an identifier never changes annotations; the
        result lists the active annotations that still use each removed
        identifier.

        Raises:
            EntityCandidateConflictError: If the job has no staging, or a newer
                catalog is already active.
            EntityCatalogCollisionError: If another source supplies one of the
                catalog's identifiers.
        """
        job = self.lock_job(job_id)
        candidate = self._candidate(job_id)
        if candidate is None:
            raise EntityCandidateConflictError
        acquire_transaction_lock(
            self.session, LockNamespace.ENTITY_CATALOG, candidate.source_key
        )
        if candidate.publication_result is not None:
            return STORED_ENTITY_REFRESH_RESULT.load(candidate.publication_result)
        if candidate.active or job.status not in ACTIVE_JOB_STATUSES:
            raise EntityCandidateConflictError
        current = self.session.scalar(
            select(EntityCatalogSnapshotRecord)
            .where(
                EntityCatalogSnapshotRecord.source_key == candidate.source_key,
                EntityCatalogSnapshotRecord.active.is_(True),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current is not None and current.staged_at > candidate.staged_at:
            raise EntityCandidateConflictError
        staging = self._verify_staging(candidate)
        rows = staging.rows
        new_ids = set(staging.db_object_ids)
        old_ids = set(
            self.session.scalars(
                select(EntityMembershipRecord.db_object_id)
                .where(
                    EntityMembershipRecord.source_key == candidate.source_key,
                )
                .order_by(EntityMembershipRecord.db_object_id)
                .with_for_update()
            )
        )
        collisions = self._collisions(candidate)
        if collisions:
            raise EntityCatalogCollisionError(collisions)
        removed = old_ids - new_ids
        impacts = self._removal_impacts(removed)
        self.session.execute(
            update(EntityCatalogSnapshotRecord)
            .where(
                EntityCatalogSnapshotRecord.source_key == candidate.source_key,
                EntityCatalogSnapshotRecord.active.is_(True),
            )
            .values(active=False)
        )
        self.session.execute(
            delete(EntityMembershipRecord).where(
                EntityMembershipRecord.source_key == candidate.source_key
            )
        )
        self._insert_memberships(candidate, new_ids)
        for batch in batched(
            (
                {key: value for key, value in row.items() if key != "job_id"}
                for row in rows
            ),
            _INSERT_BATCH_SIZE,
            strict=False,
        ):
            self.session.execute(insert(EntitySourceRecord), batch)
        result = EntityRefreshResult(
            snapshot_id=candidate.snapshot_id,
            source_key=candidate.source_key,
            source_type=candidate.source_type,
            source_locator=candidate.source_locator,
            source_revision=candidate.source_revision,
            source_checksum=candidate.source_checksum,
            source_record_count=len(rows),
            active_identifier_count=len(new_ids),
            added_count=len(new_ids - old_ids),
            retained_count=len(new_ids & old_ids),
            removed_count=len(removed),
            warnings=staging.warnings,
            removal_impacts=impacts,
        )
        candidate.active = True
        candidate.published_at = datetime.now(UTC)
        candidate.publication_result = result.to_job_result()
        self.session.flush([candidate])
        self.session.execute(
            delete(EntityStagingRecord).where(EntityStagingRecord.job_id == job_id)
        )
        return result

    def completed(self, job_id: UUID) -> EntityRefreshResult | None:
        """Return a job's stored publication result, or `None` if it has not published.

        The result stays available after a later refresh replaces the catalog.

        Raises:
            EntityCandidateConflictError: If the candidate is active but has no
                stored result.
            StoredDataError: If the stored result is malformed.
        """
        candidate = self._candidate(job_id)
        if candidate is None:
            return None
        if candidate.active and candidate.publication_result is None:
            raise EntityCandidateConflictError
        return (
            None
            if candidate.publication_result is None
            else STORED_ENTITY_REFRESH_RESULT.load(candidate.publication_result)
        )

    def active_snapshot(self, source_key: str) -> EntityCatalogSnapshotRecord | None:
        """Return the source's active snapshot without locking it."""
        return self.session.scalar(
            select(EntityCatalogSnapshotRecord).where(
                EntityCatalogSnapshotRecord.source_key == source_key,
                EntityCatalogSnapshotRecord.active.is_(True),
            )
        )

    def resumable(self, job_id: UUID) -> bool:
        """Report whether a job has committed staging that can be published.

        When a job runs again after an interruption, the worker calls this before
        fetching the source, so existing staging is published instead of fetched
        again. The staging must belong to the job's source. Its URL and checksum
        are not compared, because the registry entry may have changed since it
        was staged. The job's row stays locked, and `publish` checks every staged
        row in the same transaction.

        Raises:
            EntityCandidateConflictError: If the staging belongs to another source.
        """
        job = self.lock_job(job_id)
        candidate = self._candidate(job_id)
        if candidate is None:
            return False
        if candidate.source_key != job.parameters.get("source_key"):
            raise EntityCandidateConflictError
        return True

    def cleanup_terminal_staging(self, job_id: UUID) -> None:
        """Delete the staging rows of a job that has finished.

        Staging for a queued or running job is kept so the job can resume. This
        also removes staging left behind when a worker stopped after marking the
        job failed but before deleting it. Snapshots, jobs, and audit events are
        kept.
        """
        job = self.lock_job(job_id)
        if job.status in TERMINAL_JOB_STATUSES:
            self.session.execute(
                delete(EntityStagingRecord).where(EntityStagingRecord.job_id == job_id)
            )

    def require_active(self, db_object_id: str) -> EntityMembershipRecord:
        """Confirm an active identifier and block its removal for this transaction.

        Annotation writes call this before saving an annotation. It locks the
        identifier's membership row until the annotation's transaction ends,
        so a catalog publication or retirement cannot remove the identifier while the
        annotation is being saved. A publication that would remove it waits for the
        annotation to commit, then lists that annotation in its removal report. An
        annotation write that starts after the removal fails here.

        The lock is a PostgreSQL `FOR KEY SHARE` row lock. It does not block other
        annotation writes that check the same identifier.

        The retry loop handles one race. Publication of an entity catalog replaces a
        source's membership rows by deleting all of them and inserting the new set,
        so an identifier that stays in the catalog gets a new row. If this method was
        waiting to lock the old row, PostgreSQL reports no row once the publication
        commits, because the waiting query cannot see rows inserted after it started.
        A second, ordinary query then checks whether the identifier exists now: if it
        does, the identifier was kept and the lock is attempted again on the new row;
        if it does not, the identifier was removed.

        Args:
            db_object_id: Exact, case-sensitive identifier to check.

        Returns:
            The locked membership row.

        Raises:
            UnknownDbObjectIdError: The identifier is not in the active catalog.
        """
        statement = (
            select(EntityMembershipRecord)
            .where(
                EntityMembershipRecord.db_object_id == db_object_id,
            )
            .with_for_update(read=True, key_share=True)
            .execution_options(populate_existing=True)
        )
        while True:
            record = self.session.scalar(statement)
            if record is not None:
                return record
            still_present = self.session.scalar(
                select(EntityMembershipRecord.db_object_id).where(
                    EntityMembershipRecord.db_object_id == db_object_id
                )
            )
            if still_present is None:
                raise UnknownDbObjectIdError(db_object_id)

    def _candidate(self, job_id: UUID) -> EntityCatalogSnapshotRecord | None:
        return self.session.scalar(
            select(EntityCatalogSnapshotRecord).where(
                EntityCatalogSnapshotRecord.job_id == job_id
            )
        )

    def _verify_staging(
        self, candidate: EntityCatalogSnapshotRecord
    ) -> _VerifiedStaging:
        """Return the staging if it matches the candidate's stored counts and checksum.

        Rows are read in line-number order. The stored record counts and catalog
        checksum, which covers every line number and entity, detect missing,
        added, or changed rows.
        """
        records = self.session.scalars(
            select(EntityStagingRecord)
            .where(EntityStagingRecord.job_id == candidate.job_id)
            .order_by(EntityStagingRecord.line_number)
        ).all()
        rows: list[dict[str, object]] = [
            {
                "line_number": record.line_number,
                "db_object_id": record.db_object_id,
                "entity": record.entity,
            }
            for record in records
        ]
        db_object_ids = frozenset(record.db_object_id for record in records)
        warnings = candidate.record_statistics.get("warnings")
        if not isinstance(warnings, list) or not all(
            isinstance(item, str) for item in warnings
        ):
            raise EntityCandidateConflictError
        if (
            len(rows) != candidate.record_statistics.get("source_record_count")
            or len(db_object_ids)
            != candidate.record_statistics.get("active_identifier_count")
            or _catalog_digest(
                candidate.source_format,
                candidate.source_metadata,
                rows,
                tuple(warnings),
            )
            != candidate.source_statistics.get("catalog_sha256")
        ):
            raise EntityCandidateConflictError
        return _VerifiedStaging(rows, db_object_ids, tuple(warnings))

    def _collisions(self, candidate: EntityCatalogSnapshotRecord) -> tuple[str, ...]:
        return tuple(
            self.session.scalars(
                select(EntityMembershipRecord.db_object_id)
                .where(
                    EntityMembershipRecord.source_key != candidate.source_key,
                    EntityMembershipRecord.db_object_id.in_(
                        select(EntityStagingRecord.db_object_id).where(
                            EntityStagingRecord.job_id == candidate.job_id
                        )
                    ),
                )
                .order_by(EntityMembershipRecord.db_object_id)
            )
        )

    def _insert_memberships(
        self, candidate: EntityCatalogSnapshotRecord, identifiers: set[str]
    ) -> None:
        """Insert membership rows, rejecting identifiers another source supplies.

        `db_object_id` is the primary key of `entity_membership`, so an
        identifier can be active in only one source. `publish` checks for
        collisions first, but two sources publishing the same new identifier at
        the same time can both pass that check. The primary key then lets only
        one insert succeed, and this method reports a collision for the other,
        whose previous catalog stays active.

        Raises:
            EntityCatalogCollisionError: Another source already supplies an
                identifier in this candidate.
        """
        collided = False
        try:
            with self.session.begin_nested():
                for batch in batched(
                    (
                        {
                            "db_object_id": identifier,
                            "source_key": candidate.source_key,
                            "snapshot_id": candidate.snapshot_id,
                        }
                        for identifier in sorted(identifiers)
                    ),
                    _INSERT_BATCH_SIZE,
                    strict=False,
                ):
                    self.session.execute(insert(EntityMembershipRecord), batch)
        except IntegrityError as error:
            if (
                not isinstance(error.orig, UniqueViolation)
                or error.orig.diag.constraint_name != "pk_entity_membership"
            ):
                raise
            collided = True
        if collided:
            raise EntityCatalogCollisionError(self._collisions(candidate))

    def _removal_impacts(self, removed: set[str]) -> tuple[EntityRemovalImpact, ...]:
        impacts: dict[str, list[UUID]] = {
            identifier: [] for identifier in sorted(removed)
        }
        for batch in batched(
            ({"id": identifier} for identifier in sorted(removed)),
            _INSERT_BATCH_SIZE,
            strict=False,
        ):
            rows = self.session.execute(
                select(AnnotationRecord.db_object_id, AnnotationRecord.annotation_id)
                .where(
                    AnnotationRecord.db_object_id.in_([row["id"] for row in batch]),
                    AnnotationRecord.status == AnnotationStatus.ACTIVE,
                )
                .order_by(AnnotationRecord.db_object_id, AnnotationRecord.annotation_id)
            )
            for identifier, annotation_id in rows:
                impacts[identifier].append(annotation_id)
        return tuple(
            EntityRemovalImpact(identifier, tuple(ids))
            for identifier, ids in impacts.items()
        )


def _catalog_digest(
    source_format: str,
    source_metadata: dict[str, object],
    rows: list[dict[str, object]],
    warnings: tuple[str, ...],
) -> str:
    """Return a SHA-256 checksum of a catalog's format, metadata, warnings, and rows.

    Rows are hashed in line order. The checksum is stored as `catalog_sha256` so
    later steps can detect staging that changed after it was written.
    """
    hasher = hashlib.sha256()
    for value in (source_format, source_metadata, list(warnings)):
        hasher.update(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        hasher.update(b"\n")
    for row in rows:
        payload = {key: value for key, value in row.items() if key != "job_id"}
        hasher.update(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        hasher.update(b"\n")
    return hasher.hexdigest()
