"""Run entity refresh jobs for `RefreshRunner` from GPI sources.

A refresh reads the fetched GPI file with `ExchangeDocument` and rejects each row
that cannot be read or fails schema validation, each row whose identifier another
row of the file also lists, and each row whose identifier another source's
catalog has active. It stores the remaining rows as a snapshot and activates it in
the job's transaction. A file with no row left to load fails the job.

A file that the active catalog already holds is skipped, unless the catalog's rows
were checked with another schema package version or its activation rejected a row
as a collision. Sources removed from `config/sources.yaml` are retired when every
entity source is refreshed (see `RefreshStartService`).
"""

from go_standard_annotation_schema.datamodel import Entity
from go_standard_annotation_schema.io import GpiReader, Record

from standard_annotation_backend.domain.entities import (
    GPI_SOURCE_FORMAT,
    EntityCatalog,
    EntityRefreshResult,
    EntityRefreshUnchangedResult,
    reject_repeated_identifiers,
)
from standard_annotation_backend.domain.jobs import JobType, ProgressReporter
from standard_annotation_backend.domain.refresh import (
    NoValidRowsError,
    RefreshKindName,
    RefreshOutcome,
    RejectionReportBuilder,
    RowRejection,
    SourceDocument,
)
from standard_annotation_backend.domain.schema_artifacts import SCHEMA_PACKAGE_VERSION
from standard_annotation_backend.domain.snapshots import SnapshotKind, SourceContent
from standard_annotation_backend.exchange_files import ExchangeDocument
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork
from standard_annotation_backend.refresh.decoding import decode_source_text
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job


class EntityRefreshService:
    """Refresh entity catalogs, recording an audit event for each activation.

    Implements `RefreshKind`.
    """

    @property
    def name(self) -> RefreshKindName:
        """Return `entity`."""
        return RefreshKindName.ENTITY

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `entity_refresh`."""
        return frozenset({JobType.ENTITY_REFRESH})

    def apply(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        job: Job,
        document: SourceDocument,
        report: ProgressReporter,
    ) -> RefreshOutcome:
        """Store and activate the file's valid entities unless they are already active.

        A file that the active catalog already holds, read with the same reader
        version, is not stored again. The result then names the job that
        activated that catalog; that job's result lists the file's rejected rows.
        The file is read again anyway when the catalog's rows were checked with
        another schema package version, or when its activation rejected a row as a
        collision, because either can change which rows are loaded (see
        `EntitySnapshotDetail.reusable`). Either way, entity catalog snapshots
        that are no longer kept are pruned.

        Raises:
            SourceError: If the bytes are not valid gzip or UTF-8.
            ExchangeHeaderError: If the GPI header is invalid.
            NoValidRowsError: If no row can be loaded, including when another
                source has every valid row's identifier active.
        """
        snapshots = unit_of_work.snapshots
        content = SourceContent.of(RefreshKindName.ENTITY, document.provenance)
        active = snapshots.matching_active(
            SnapshotKind.ENTITY, document.source_key, content
        )
        detail = (
            None
            if active is None
            else unit_of_work.entities.snapshot_detail(active.snapshot_id)
        )
        if active is not None and detail is not None and detail.reusable():
            snapshots.prune(SnapshotKind.ENTITY)
            unchanged = EntityRefreshUnchangedResult(
                source_key=document.source_key,
                snapshot_id=active.snapshot_id,
                snapshot_job_id=active.job_id,
                source_checksum=document.source_checksum,
            )
            return RefreshOutcome(result=unchanged.to_job_result(), unchanged=True)
        rejections = RejectionReportBuilder()
        catalog, data_rows = _read_catalog(
            decode_source_text(document.content), rejections
        )
        counts = {
            "data_rows": data_rows,
            "entity_count": len(catalog.records),
            "records_rejected": rejections.build().issue_count,
        }
        report("storing", counts)
        record = snapshots.create(
            SnapshotKind.ENTITY, document.source_key, job.job_id, document.provenance
        )
        unit_of_work.entities.insert(record.snapshot_id, catalog)
        report("activating", counts)
        unit_of_work.entities.lock_activation()
        collisions = unit_of_work.entities.reject_collisions(
            record.snapshot_id, document.source_key
        )
        for collision in collisions:
            rejections.add(collision.to_rejection())
        new_ids = {entry.item.db_object_id for entry in catalog.records} - {
            collision.db_object_id for collision in collisions
        }
        if not new_ids:
            raise NoValidRowsError(rejections.build())
        previous = snapshots.activate(record)
        old_ids = (
            set()
            if previous is None
            else unit_of_work.entities.identifiers(previous.snapshot_id)
        )
        rejection_report = rejections.build()
        result = EntityRefreshResult(
            snapshot_id=record.snapshot_id,
            source_key=document.source_key,
            source_type=record.source_type,
            source_locator=record.source_locator,
            source_revision=record.source_revision,
            source_checksum=record.source_checksum,
            data_rows=data_rows,
            entity_count=len(new_ids),
            records_rejected=rejection_report.issue_count,
            added_count=len(new_ids - old_ids),
            retained_count=len(new_ids & old_ids),
            removed_count=len(old_ids - new_ids),
            rejection_report=rejection_report,
        )
        AuditService(unit_of_work.audit).record_entity_refreshed(
            actor_id=job.requested_by,
            job_id=job.job_id,
            details={
                "snapshot_id": str(result.snapshot_id),
                "source_key": result.source_key,
                **result.to_progress(),
            },
        )
        snapshots.prune(SnapshotKind.ENTITY)
        return RefreshOutcome(
            result=result.to_job_result(), counts=result.to_progress()
        )


def _read_catalog(
    text: str, rejections: RejectionReportBuilder
) -> tuple[EntityCatalog, int]:
    """Read a GPI document's valid entities, adding each rejected row to `rejections`.

    Returns:
        The catalog of valid entities and the number of data rows read.

    Raises:
        ExchangeHeaderError: If the header is invalid.
        NoValidRowsError: If no row can be loaded.
    """
    records: list[Record[Entity]] = []
    with ExchangeDocument(GpiReader, text) as document:
        for item in document.items():
            if isinstance(item, RowRejection):
                rejections.add(item)
            else:
                records.append(item)
        metadata = document.metadata
        data_rows = document.data_rows
    kept, repeated = reject_repeated_identifiers(records)
    for rejection in repeated:
        rejections.add(rejection)
    if not kept:
        raise NoValidRowsError(rejections.build())
    catalog = EntityCatalog(
        source_format=GPI_SOURCE_FORMAT,
        source_metadata=metadata,
        schema_version=SCHEMA_PACKAGE_VERSION,
        records=kept,
    )
    return catalog, data_rows
