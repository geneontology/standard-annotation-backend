"""Stage, publish, and retire entity catalogs, each in its own transaction."""

from uuid import UUID

from standard_annotation_backend.domain.entities import (
    EntityCatalog,
    EntityCatalogRetirementResult,
    EntityRefreshResult,
    EntityRefreshUnchangedResult,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.audit_service import AuditService


class EntityRefreshService:
    """Stage, publish, and retire catalogs, committing each with its audit events."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def stage(
        self,
        *,
        job_id: UUID,
        source_key: str,
        catalog: EntityCatalog,
    ) -> None:
        """Store a parsed catalog as a job's inactive staging and commit it.

        If the job already staged the same catalog, the existing staging is kept.

        Raises:
            EntityCandidateConflictError: If the job already staged a different
                catalog, or the job has finished.
        """
        with self._unit_of_work_factory() as uow:
            uow.entities.stage(job_id, source_key, catalog)
            uow.commit()

    def retire(
        self, *, job_id: UUID, source_key: str, configured: bool, actor_id: str
    ) -> EntityCatalogRetirementResult:
        """Retire a source's active catalog and record its audit event, then commit.

        If this job already retired the catalog, for example before its task was
        delivered again, the stored result is returned and no audit event is added.
        """
        with self._unit_of_work_factory() as uow:
            result, changed = uow.entities.retire(
                job_id, source_key, configured=configured
            )
            if changed:
                AuditService(uow.audit).record_entity_retired(
                    actor_id=actor_id,
                    job_id=job_id,
                    details={
                        "source_key": source_key,
                        "snapshot_id": str(result.snapshot_id),
                        "removed_count": len(result.removal_impacts),
                    },
                )
            uow.commit()
            return result

    def unchanged(
        self, *, source_key: str, source_locator: str, source_checksum: str
    ) -> EntityRefreshUnchangedResult | None:
        """Return a result when the source's active catalog has identical content.

        Both the locator and the checksum must match, so pointing a source at a new
        location always publishes a new snapshot.
        """
        with self._unit_of_work_factory() as uow:
            snapshot = uow.entities.active_snapshot(source_key)
            if (
                snapshot is None
                or snapshot.source_locator != source_locator
                or snapshot.source_checksum != source_checksum
            ):
                return None
            return EntityRefreshUnchangedResult(
                source_key=source_key,
                snapshot_id=snapshot.snapshot_id,
                source_checksum=source_checksum,
            )

    def publish(self, *, job_id: UUID, actor_id: str) -> EntityRefreshResult:
        """Make a job's staged catalog active and record its audit event, then commit.

        If this job already published its catalog, the stored result is returned
        and no audit event is added.
        """
        with self._unit_of_work_factory() as uow:
            return self._publish(uow, job_id=job_id, actor_id=actor_id)

    def resume(self, *, job_id: UUID, actor_id: str) -> EntityRefreshResult | None:
        """Publish a job's committed staging without fetching the source again.

        A worker calls this when a job runs again after an interruption. It
        returns `None` when the job has no staging, so the worker fetches the
        source. Staged rows are checked against the stored record counts and
        catalog checksum in the same transaction that publishes them.

        Raises:
            EntityCandidateConflictError: If the staging belongs to another source
                or is incomplete or altered. The active catalog is not changed.
        """
        with self._unit_of_work_factory() as uow:
            if not uow.entities.resumable(job_id):
                return None
            return self._publish(uow, job_id=job_id, actor_id=actor_id)

    def _publish(
        self, uow: SqlAlchemyUnitOfWork, *, job_id: UUID, actor_id: str
    ) -> EntityRefreshResult:
        """Publish in an open unit of work, record the audit event, and commit."""
        uow.entities.lock_job(job_id)
        completed = uow.entities.completed(job_id)
        if completed is not None:
            return EntityRefreshResult.from_job_result(completed)
        result = uow.entities.publish(job_id)
        AuditService(uow.audit).record_entity_refreshed(
            actor_id=actor_id,
            job_id=job_id,
            details={
                "snapshot_id": str(result.snapshot_id),
                "source_key": result.source_key,
                "source_record_count": result.source_record_count,
                "active_identifier_count": result.active_identifier_count,
                "added_count": result.added_count,
                "retained_count": result.retained_count,
                "removed_count": result.removed_count,
                "warning_count": len(result.warnings),
            },
        )
        uow.commit()
        return result

    def completed(self, job_id: UUID) -> dict[str, object] | None:
        """Return a job's stored publication result, or `None` if not yet published."""
        with self._unit_of_work_factory() as uow:
            return uow.entities.completed(job_id)

    def cleanup_terminal_staging(self, job_id: UUID) -> None:
        """Delete a finished job's staging rows and commit.

        Staging for a queued or running job is kept so the job can resume.
        """
        with self._unit_of_work_factory() as uow:
            uow.entities.cleanup_terminal_staging(job_id)
            uow.commit()
