"""Set up database states directly for integration tests.

These helpers write rows without going through the production write rules: they
take no locks and enforce no entity-catalog, duplicate, or authorization checks.
Use them only in tests whose subject is something else and that need a starting
state, such as an imported annotation or a legacy duplicate peer, that the public
write paths would refuse to create.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    AnnotationStatus,
    new_annotation_id,
)
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.persistence.annotation_data import (
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.models import (
    AnnotationDuplicateReferenceRecord,
    AnnotationMultivaluedFieldValueRecord,
    AnnotationRecord,
    AnnotationVersionRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job, job_from_record

__all__ = ["create_job", "insert_annotation"]


def insert_annotation(
    session: Session,
    annotation: Annotation,
    *,
    owning_group_id: str = "MGI",
    actor_id: str = "curator",
    change_source: str = "test",
    record_origin: AnnotationOrigin = AnnotationOrigin.DIRECT,
    source_import_job_id: UUID | None = None,
    annotation_id: UUID | None = None,
) -> AnnotationRecord:
    """Insert an active annotation at version 1 with its derived lookup rows.

    The caller owns the transaction and must commit it.

    Args:
        session: Session to write with.
        annotation: Validated annotation to store.
        owning_group_id: Group responsible for the annotation.
        actor_id: Actor recorded on the first version.
        change_source: Workflow recorded on the first version.
        record_origin: How the annotation is recorded as having entered SAB.
        source_import_job_id: Import job recorded as the source, for imported rows.
        annotation_id: Identifier to use instead of generating one.

    Returns:
        The flushed current annotation record.
    """
    data = prepare_annotation_for_persistence(annotation)
    identifier = annotation_id or new_annotation_id()
    record = AnnotationRecord(
        annotation_id=identifier,
        current_version=1,
        status=AnnotationStatus.ACTIVE.value,
        deleted_at=None,
        owning_group_id=owning_group_id,
        record_origin=record_origin.value,
        source_import_job_id=source_import_job_id,
        **data.column_values(),
    )
    session.add(record)
    session.flush([record])
    session.add(
        AnnotationVersionRecord(
            annotation_id=identifier,
            version=1,
            annotation_data=data.annotation_data,
            is_deleted=False,
            actor_id=actor_id,
            change_source=change_source,
        )
    )
    session.add_all(
        AnnotationMultivaluedFieldValueRecord(annotation_id=identifier, **row)
        for row in data.multivalued_rows()
    )
    session.add_all(
        AnnotationDuplicateReferenceRecord(annotation_id=identifier, **row)
        for row in data.reference_rows()
    )
    session.flush()
    return record


def create_job(
    factory: UnitOfWorkFactory,
    *,
    job_type: JobType,
    parameters: dict[str, object],
    requested_by: str = "test",
) -> Job:
    """Create a queued job and record its queued audit event, then commit.

    Args:
        factory: Factory for the unit of work used to write the job.
        job_type: Kind of job to create.
        parameters: Parameters stored with the job.
        requested_by: Actor recorded as requesting the job.

    Returns:
        The committed queued job.
    """
    with factory() as uow:
        record = uow.jobs.create(
            job_type=job_type,
            requested_by=requested_by,
            parameters=parameters,
            now=datetime.now(UTC),
        )
        AuditService(uow.audit).record_job_lifecycle(
            action=AuditAction.JOB_QUEUED, record=record
        )
        job = job_from_record(record)
        uow.commit()
    return job
