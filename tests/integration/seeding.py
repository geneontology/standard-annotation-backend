"""Set up database states directly for integration tests.

These helpers write rows without going through the production write rules: they
take no locks and enforce no entity-catalog, duplicate, or authorization checks.
Use them only in tests whose subject is something else and that need a starting
state, such as an imported annotation or a legacy duplicate peer, that the public
write paths would refuse to create.

`unvalidated_create` and `unvalidated_update` are the exception: they take the
write locks, but skip validation, for tests that write through the repository.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
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
from standard_annotation_backend.domain.ontology import SUBCLASS_PREDICATE
from standard_annotation_backend.domain.refresh import RULES_SOURCE_KEY, RefreshKindName
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    STORED_RULE_DATA,
    DatabaseEntry,
    ExtensionConstraint,
    GoRefEntry,
    RuleData,
)
from standard_annotation_backend.domain.snapshots import (
    SnapshotKind,
    current_reader_version,
)
from standard_annotation_backend.persistence.annotation_data import (
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.models import (
    AnnotationDuplicateReferenceRecord,
    AnnotationMultivaluedFieldValueRecord,
    AnnotationRecord,
    AnnotationVersionRecord,
    EntitySnapshotDetailRecord,
    JobRecord,
    OntologyAltIdRecord,
    OntologyClosureRecord,
    OntologySnapshotDetailRecord,
    OntologyTermRecord,
    ReferenceSnapshotRecord,
    RetractedPublicationRecord,
    RuleDataRecord,
    RulesSnapshotDetailRecord,
    StoredEntityRecord,
)
from standard_annotation_backend.persistence.repositories import AnnotationRepository
from standard_annotation_backend.persistence.repositories.annotations import (
    ValidatedCreate,
    ValidatedUpdate,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.refresh.sources import (
    RefreshSources,
    load_sources_file,
)
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job, job_from_record

__all__ = [
    "DEFAULT_RULE_DATA",
    "DEFAULT_TERMS",
    "SHIPPED_SOURCES",
    "SeedTerm",
    "active_snapshot",
    "create_job",
    "deactivate_reference_data",
    "entity_identifiers",
    "insert_annotation",
    "seed_entity_catalog",
    "seed_reference_data",
    "set_rule_data",
    "snapshot_count",
    "unvalidated_create",
    "unvalidated_update",
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


@dataclass(frozen=True, slots=True)
class SeedTerm:
    """Describe one term to store in a seeded ontology snapshot.

    Attributes:
        parents: For a GO term, its direct `rdfs:subClassOf` parents. They must
            already be seeded, earlier in the same call or in an earlier call.
        subsets: The term's subsets, such as `gocheck_do_not_annotate`.
    """

    term_id: str
    obsolete: bool = False
    replaced_by: tuple[str, ...] = ()
    consider: tuple[str, ...] = ()
    alt_ids: tuple[str, ...] = ()
    parents: tuple[str, ...] = ()
    subsets: tuple[str, ...] = ()


DEFAULT_TERMS: tuple[str | SeedTerm, ...] = (
    "GO:0003674",
    "GO:0008150",
    "GO:0005575",
    SeedTerm("GO:0003824", parents=("GO:0003674",)),
    SeedTerm("GO:0006915", parents=("GO:0008150",)),
    SeedTerm("GO:0044419", parents=("GO:0008150",)),
    SeedTerm("GO:0000002", parents=("GO:0008150",)),
    SeedTerm("GO:0032991", parents=("GO:0005575",)),
    "CL:0000000",
    "CL:0000001",
    "CHEBI:1",
    "CHEBI:15422",
    "NCBITaxon:9606",
    "NCBITaxon:10090",
)
"""Current terms used by the annotation payloads of the integration tests.

The GO terms form a small hierarchy under the three roots. `GO:0006915` is an
ordinary biological process term, `GO:0003824` a molecular function term,
`GO:0044419` the term that allows interacting taxa, and `GO:0032991` the
protein-containing complex term.
"""

_EXTENSION_ROOTS = ("GO:0003674", "GO:0008150", "GO:0005575")

DEFAULT_RULE_DATA = RuleData(
    databases=tuple(
        DatabaseEntry(database)
        for database in (
            "CHEBI",
            "CL",
            "ComplexPortal",
            "EMAPA",
            "GO",
            "MGI",
            "NCBITaxon",
            "PR",
            "RGD",
            "SGD",
            "UBERON",
            "UniProtKB",
            "WB",
            "Xenbase",
            "ZFIN",
        )
    ),
    go_refs=(GoRefEntry("GO_REF:0000015"), GoRefEntry("GO_REF:0000033")),
    group_shorthands=("GO_Central", "MGI", "RGD", "SGD", "WB", "Xenbase", "ZFIN"),
    eco_usage_constraints=(),
    extension_constraints=(
        ExtensionConstraint(
            "BFO:0000050", ("CL", "EMAPA", "GO", "UBERON"), _EXTENSION_ROOTS
        ),
        ExtensionConstraint(
            "BFO:0000066", ("CL", "EMAPA", "GO", "UBERON"), _EXTENSION_ROOTS
        ),
        ExtensionConstraint(
            "RO:0002233", ("CHEBI", "GO", "MGI", "PR", "UniProtKB"), _EXTENSION_ROOTS
        ),
    ),
)
"""Permissive go-site data that the integration-test payloads satisfy.

The databases have no identifier syntax, there are no evidence usage
constraints, and the extension constraints have no cardinality, so only the
prefixes, groups, and extension relations the tests use are restricted.
"""


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


def _seeded_snapshot(
    session: Session, kind: SnapshotKind, source_key: str, job_type: str
) -> ReferenceSnapshotRecord:
    """Add an active snapshot of a source, owned by a new succeeded job."""
    now = datetime.now(UTC)
    record = ReferenceSnapshotRecord(
        snapshot_id=uuid4(),
        kind=kind,
        source_key=source_key,
        job_id=_succeeded_job(session, job_type, source_key),
        source_type="https",
        source_locator=f"https://example.org/{source_key}",
        source_revision=None,
        source_checksum="b" * 64,
        fetched_at=now,
        reader_version=current_reader_version(RefreshKindName(kind.value)),
        active=True,
        activated_at=now,
    )
    session.add(record)
    session.flush([record])
    return record


def _add_subclass_closure(session: Session, snapshot_id: UUID, seed: SeedTerm) -> None:
    """Store a new GO term's subclass closure: itself and every ancestor.

    Each parent's own closure rows supply the ancestors, so parents must be
    stored first.
    """
    depths = {seed.term_id: 0}
    for parent in seed.parents:
        rows = session.execute(
            select(
                OntologyClosureRecord.object_term_id, OntologyClosureRecord.depth
            ).where(
                OntologyClosureRecord.snapshot_id == snapshot_id,
                OntologyClosureRecord.predicate_id == SUBCLASS_PREDICATE,
                OntologyClosureRecord.subject_term_id == parent,
            )
        )
        for ancestor, depth in rows:
            depths[ancestor] = min(depths.get(ancestor, depth + 1), depth + 1)
    session.add_all(
        OntologyClosureRecord(
            snapshot_id=snapshot_id,
            subject_term_id=seed.term_id,
            predicate_id=SUBCLASS_PREDICATE,
            object_term_id=ancestor,
            depth=depth,
        )
        for ancestor, depth in depths.items()
    )
    session.flush()


def seed_reference_data(
    session_factory: sessionmaker[Session], *terms: str | SeedTerm
) -> None:
    """Make the reference data that validated writes need active, with `terms`.

    Every ontology in the shipped sources gets an active snapshot, unless it already
    has one, and so does the go-site rule data, which is `DEFAULT_RULE_DATA`. Each
    term is added to the snapshot of the ontology that owns its prefix, unless that
    snapshot already has it; its alternative identifiers are added either way.
    Calling this again adds more terms to the same snapshots.

    Args:
        session_factory: Factory for sessions on the test database.
        terms: Term identifiers, which are stored as current terms, or
            `SeedTerm`s for obsolete terms, alternative identifiers, GO
            parents, and subsets. A new GO term also gets its
            `rdfs:subClassOf` closure.
    """
    seeds = [SeedTerm(term) if isinstance(term, str) else term for term in terms]
    with session_factory() as session:
        for key in SHIPPED_SOURCES.keys(RefreshKindName.ONTOLOGY):
            definition = SHIPPED_SOURCES.ontology_definition(key)
            snapshot = session.scalar(
                select(ReferenceSnapshotRecord).where(
                    ReferenceSnapshotRecord.kind == SnapshotKind.ONTOLOGY,
                    ReferenceSnapshotRecord.source_key == key,
                    ReferenceSnapshotRecord.active.is_(True),
                )
            )
            if snapshot is None:
                snapshot = _seeded_snapshot(
                    session, SnapshotKind.ONTOLOGY, key, "ontology_refresh"
                )
                session.add(
                    OntologySnapshotDetailRecord(
                        snapshot_id=snapshot.snapshot_id,
                        document_version=None,
                        loaded_predicates=list(definition.closure_predicates),
                        identifier_prefixes=list(definition.identifier_prefixes),
                    )
                )
                session.flush()
            stored = set(
                session.scalars(
                    select(OntologyTermRecord.term_id).where(
                        OntologyTermRecord.snapshot_id == snapshot.snapshot_id
                    )
                )
            )
            stored_alt_ids = set(
                session.scalars(
                    select(OntologyAltIdRecord.alt_id).where(
                        OntologyAltIdRecord.snapshot_id == snapshot.snapshot_id
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
                    snapshot_id=snapshot.snapshot_id,
                    term_id=seed.term_id,
                    obsolete=seed.obsolete,
                    replaced_by=list(seed.replaced_by),
                    consider=list(seed.consider),
                    subsets=list(seed.subsets),
                )
                for seed in owned
                if seed.term_id not in stored
            )
            session.flush()
            session.add_all(
                OntologyAltIdRecord(
                    snapshot_id=snapshot.snapshot_id,
                    alt_id=alt_id,
                    term_id=seed.term_id,
                )
                for seed in owned
                for alt_id in seed.alt_ids
                if alt_id not in stored_alt_ids
            )
            if SUBCLASS_PREDICATE in definition.closure_predicates:
                for seed in owned:
                    if seed.term_id not in stored:
                        _add_subclass_closure(session, snapshot.snapshot_id, seed)
        rules = session.scalar(
            select(ReferenceSnapshotRecord).where(
                ReferenceSnapshotRecord.kind == SnapshotKind.RULES,
                ReferenceSnapshotRecord.active.is_(True),
            )
        )
        if rules is None:
            rules = _seeded_snapshot(
                session, SnapshotKind.RULES, RULES_SOURCE_KEY, "rules_refresh"
            )
            # Read from go-site at a commit, as a rules refresh records it.
            rules.source_type = "github"
            rules.source_locator = "github:geneontology/go-site"
            rules.source_revision = "c" * 40
            session.add_all(
                [
                    RulesSnapshotDetailRecord(
                        snapshot_id=rules.snapshot_id, retracted_publication_count=0
                    ),
                    RuleDataRecord(
                        snapshot_id=rules.snapshot_id,
                        data=STORED_RULE_DATA.dump(DEFAULT_RULE_DATA),
                    ),
                ]
            )
        session.commit()


def set_rule_data(
    session_factory: sessionmaker[Session],
    rule_data: RuleData,
    *,
    retracted_pmids: Iterable[str] = (),
) -> None:
    """Replace the active go-site rule data and its retracted PMIDs.

    Call it after `seed_reference_data`, which creates the active rules snapshot.

    Args:
        session_factory: Factory for sessions on the test database.
        rule_data: The rule data to store.
        retracted_pmids: PMIDs, such as `PMID:9`, to list as retracted.
    """
    pmids = sorted(set(retracted_pmids))
    with session_factory.begin() as session:
        snapshot = session.scalars(
            select(ReferenceSnapshotRecord).where(
                ReferenceSnapshotRecord.kind == SnapshotKind.RULES,
                ReferenceSnapshotRecord.active.is_(True),
            )
        ).one()
        stored = session.get_one(RuleDataRecord, snapshot.snapshot_id)
        stored.data = STORED_RULE_DATA.dump(rule_data)
        detail = session.get_one(RulesSnapshotDetailRecord, snapshot.snapshot_id)
        detail.retracted_publication_count = len(pmids)
        session.execute(
            delete(RetractedPublicationRecord).where(
                RetractedPublicationRecord.snapshot_id == snapshot.snapshot_id
            )
        )
        session.add_all(
            RetractedPublicationRecord(snapshot_id=snapshot.snapshot_id, pmid=pmid)
            for pmid in pmids
        )


def seed_entity_catalog(
    session_factory: sessionmaker[Session],
    identifiers: Iterable[str],
    *,
    db_object_type: str = "PR:000000001",
    db_object_taxon_id: str = "NCBITaxon:7955",
    source_key: str | None = None,
) -> UUID:
    """Make `identifiers` active as one new entity catalog and return its snapshot.

    The rows are written directly, so no refresh audit events are recorded.
    """
    key = source_key or f"seed-{uuid4().hex}"
    with session_factory.begin() as session:
        record = _seeded_snapshot(session, SnapshotKind.ENTITY, key, "entity_refresh")
        session.add(
            EntitySnapshotDetailRecord(
                snapshot_id=record.snapshot_id,
                source_format="gpi-2.0",
                source_metadata={},
                record_statistics={},
            )
        )
        session.add_all(
            StoredEntityRecord(
                snapshot_id=record.snapshot_id,
                db_object_id=identifier,
                line_number=line_number,
                entity={
                    "db_object_id": identifier,
                    "db_object_type": db_object_type,
                    "db_object_taxon_id": db_object_taxon_id,
                    "canonical_object_id": identifier,
                },
                db_object_taxon_id=db_object_taxon_id,
                db_object_type=db_object_type,
            )
            for line_number, identifier in enumerate(identifiers, start=1)
        )
        return record.snapshot_id


def deactivate_reference_data(
    session_factory: sessionmaker[Session], kind: SnapshotKind, source_key: str
) -> None:
    """Leave a source without an active snapshot, as if it had never been loaded."""
    with session_factory.begin() as session:
        session.execute(
            update(ReferenceSnapshotRecord)
            .where(
                ReferenceSnapshotRecord.kind == kind,
                ReferenceSnapshotRecord.source_key == source_key,
                ReferenceSnapshotRecord.active.is_(True),
            )
            .values(active=False)
        )


def active_snapshot(
    session_factory: sessionmaker[Session], kind: SnapshotKind, source_key: str
) -> ReferenceSnapshotRecord | None:
    """Return a source's active snapshot, detached from its session."""
    with session_factory() as session:
        return session.scalar(
            select(ReferenceSnapshotRecord).where(
                ReferenceSnapshotRecord.kind == kind,
                ReferenceSnapshotRecord.source_key == source_key,
                ReferenceSnapshotRecord.active.is_(True),
            )
        )


def snapshot_count(session_factory: sessionmaker[Session], kind: SnapshotKind) -> int:
    """Return how many snapshots of a kind are stored, pruned or not."""
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(ReferenceSnapshotRecord)
                .where(ReferenceSnapshotRecord.kind == kind)
            )
            or 0
        )


def entity_identifiers(
    session_factory: sessionmaker[Session], source_key: str
) -> set[str]:
    """Return the identifiers of a source's active entity catalog."""
    with session_factory() as session:
        return set(
            session.scalars(
                select(StoredEntityRecord.db_object_id)
                .join(
                    ReferenceSnapshotRecord,
                    ReferenceSnapshotRecord.snapshot_id
                    == StoredEntityRecord.snapshot_id,
                )
                .where(
                    ReferenceSnapshotRecord.kind == SnapshotKind.ENTITY,
                    ReferenceSnapshotRecord.source_key == source_key,
                    ReferenceSnapshotRecord.active.is_(True),
                )
            )
        )


def unvalidated_create(
    repository: AnnotationRepository, annotation: Annotation
) -> ValidatedCreate:
    """Lock a new annotation for `create` without validating it.

    Use it only where validation is not the subject and would refuse the
    starting state, for example a duplicate or a test without reference data.
    """
    return ValidatedCreate(repository.lock_for_create(annotation))


def unvalidated_update(
    repository: AnnotationRepository,
    annotation_id: UUID,
    annotation: Annotation,
    *,
    expected_version: int,
) -> ValidatedUpdate:
    """Lock an annotation and its replacement for `update` without validating it.

    Raises the same not-found, deleted, and stale-version errors as
    `AnnotationRepository.lock_for_update`.
    """
    return ValidatedUpdate(
        repository.lock_for_update(
            annotation_id, annotation, expected_version=expected_version
        )
    )
