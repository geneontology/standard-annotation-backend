"""Set up database states directly for integration tests.

These helpers write rows without going through the production write rules: they
take no locks and enforce no entity-catalog, duplicate, or authorization checks.
Use them only in tests whose subject is something else and that need a starting
state, such as an imported annotation or a legacy duplicate peer, that the public
write paths would refuse to create.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    AnnotationStatus,
    ChangeSource,
    new_annotation_id,
)
from standard_annotation_backend.domain.audit import AuditAction
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.refresh import RefreshKindName
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    STORED_RULE_DATA,
    RuleData,
)
from standard_annotation_backend.persistence.annotation_data import (
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.models import (
    AnnotationDuplicateReferenceRecord,
    AnnotationMultivaluedFieldValueRecord,
    AnnotationRecord,
    AnnotationVersionRecord,
    JobRecord,
    OntologyAltIdRecord,
    OntologyMetadataRecord,
    OntologyTermRecord,
    RulesSnapshotRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh.sources import (
    RefreshSources,
    load_sources_file,
)
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job, job_from_record

__all__ = [
    "DEFAULT_TERMS",
    "SHIPPED_SOURCES",
    "SeedTerm",
    "create_job",
    "insert_annotation",
    "seed_reference_data",
]


def insert_annotation(
    session: Session,
    annotation: Annotation,
    *,
    owning_group_id: str = "MGI",
    actor_id: str = "curator",
    change_source: ChangeSource = ChangeSource.API,
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
        record_origin: How the annotation is recorded as having entered SABE.
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


SHIPPED_SOURCES = RefreshSources(
    load_sources_file(Path(__file__).parents[2] / "config" / "sources.yaml")
)
"""The shipped sources, whose ontologies every validated write needs."""

DEFAULT_TERMS = (
    "GO:0003674",
    "GO:0005575",
    "GO:0008150",
    "GO:0000002",
    "CL:0000000",
    "CL:0000001",
    "CHEBI:1",
    "CHEBI:15422",
    "NCBITaxon:9606",
    "NCBITaxon:10090",
)
"""Current terms used by the annotation payloads of the integration tests."""


@dataclass(frozen=True, slots=True)
class SeedTerm:
    """Describe one term to store in a seeded ontology snapshot."""

    term_id: str
    obsolete: bool = False
    replaced_by: tuple[str, ...] = ()
    consider: tuple[str, ...] = ()
    alt_ids: tuple[str, ...] = ()


def _succeeded_job(session: Session, job_type: str, source_key: str) -> UUID:
    """Add a succeeded job that a seeded snapshot can belong to."""
    now = datetime.now(UTC)
    job = JobRecord(
        job_id=uuid4(),
        job_type=job_type,
        status="succeeded",
        requested_by="test-supplier",
        parameters={"source_key": source_key},
        progress={},
        warnings=[],
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=now,
        result={},
    )
    session.add(job)
    session.flush([job])
    return job.job_id


def seed_reference_data(
    session_factory: sessionmaker[Session], *terms: str | SeedTerm
) -> None:
    """Make the reference data that validated writes need active, with `terms`.

    Every ontology in the shipped sources gets an active snapshot, unless it
    already has one, and so does the go-site rule data. Each term is added to the
    snapshot of the ontology that owns its prefix, unless that snapshot already
    has it; its alternative identifiers are added either way. Calling this again
    adds more terms to the same snapshots.

    Args:
        session_factory: Factory for sessions on the test database.
        terms: Term identifiers, which are stored as current terms, or
            `SeedTerm`s for obsolete terms and alternative identifiers.
    """
    seeds = [SeedTerm(term) if isinstance(term, str) else term for term in terms]
    now = datetime.now(UTC)
    with session_factory() as session:
        for key in SHIPPED_SOURCES.keys(RefreshKindName.ONTOLOGY):
            definition = SHIPPED_SOURCES.ontology_definition(key)
            snapshot = session.scalar(
                select(OntologyMetadataRecord).where(
                    OntologyMetadataRecord.ontology_key == key,
                    OntologyMetadataRecord.active.is_(True),
                )
            )
            if snapshot is None:
                snapshot = OntologyMetadataRecord(
                    version_id=uuid4(),
                    ontology_key=key,
                    source_type="https",
                    source_locator=f"https://example.org/{key}.obo",
                    source_revision=None,
                    source_checksum="b" * 64,
                    document_version=None,
                    loaded_predicates=list(definition.closure_predicates),
                    identifier_prefixes=list(definition.identifier_prefixes),
                    refresh_result=None,
                    job_id=_succeeded_job(session, "ontology_refresh", key),
                    fetched_at=now,
                    active=True,
                )
                session.add(snapshot)
                session.flush([snapshot])
            stored = set(
                session.scalars(
                    select(OntologyTermRecord.term_id).where(
                        OntologyTermRecord.version_id == snapshot.version_id
                    )
                )
            )
            stored_alt_ids = set(
                session.scalars(
                    select(OntologyAltIdRecord.alt_id).where(
                        OntologyAltIdRecord.version_id == snapshot.version_id
                    )
                )
            )
            owned = [
                seed
                for seed in seeds
                if seed.term_id.partition(":")[0] in definition.identifier_prefixes
            ]
            session.add_all(
                OntologyTermRecord(
                    version_id=snapshot.version_id,
                    term_id=seed.term_id,
                    obsolete=seed.obsolete,
                    replaced_by=list(seed.replaced_by),
                    consider=list(seed.consider),
                    subsets=[],
                )
                for seed in owned
                if seed.term_id not in stored
            )
            session.flush()
            session.add_all(
                OntologyAltIdRecord(
                    version_id=snapshot.version_id,
                    alt_id=alt_id,
                    term_id=seed.term_id,
                )
                for seed in owned
                for alt_id in seed.alt_ids
                if alt_id not in stored_alt_ids
            )
        if (
            session.scalar(
                select(RulesSnapshotRecord).where(RulesSnapshotRecord.active.is_(True))
            )
            is None
        ):
            session.add(
                RulesSnapshotRecord(
                    job_id=_succeeded_job(session, "rules_refresh", "go-site"),
                    source_type="github",
                    source_locator="github:geneontology/go-site",
                    source_revision="c" * 40,
                    source_checksum="c" * 64,
                    fetched_at=now,
                    active=True,
                    rule_data=STORED_RULE_DATA.dump(RuleData((), (), (), (), ())),
                    retracted_publication_count=0,
                )
            )
        session.commit()
