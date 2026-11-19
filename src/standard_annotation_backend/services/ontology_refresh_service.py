"""Refresh ontologies from configured OBO sources.

A refresh parses the OBO file, stores a new snapshot of it, and makes that
snapshot active, all in the job's transaction. Activation changes no
annotations; it queues a term replacement job for all groups, which replaces
obsolete terms and alternative IDs in annotations later. Every ontology job then
prunes ontology snapshots that are no longer kept (see
`persistence.repositories.snapshots`).
"""

from dataclasses import replace

from standard_annotation_backend.domain.jobs import (
    JobFailureCode,
    JobType,
    ProgressReporter,
)
from standard_annotation_backend.domain.ontology import (
    STORED_ONTOLOGY_REFRESH_RESULT,
    OntologyDefinition,
    OntologyDocument,
    OntologyParseError,
    OntologyRefreshResult,
    OntologySnapshot,
    OntologyVersion,
    StoredOntologyRefresh,
    compute_closure,
    missing_required_subsets,
    ontology_refresh_warnings,
)
from standard_annotation_backend.domain.refresh import (
    RefreshKindName,
    RefreshOutcome,
    SourceDocument,
    TerminalRefreshError,
)
from standard_annotation_backend.domain.snapshots import SnapshotKind, SourceContent
from standard_annotation_backend.ontology.obo_parser import parse_obo
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from standard_annotation_backend.refresh.sources import RefreshSources
from standard_annotation_backend.services.annotation_scan import queue_or_join
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job


class OntologyRefreshService:
    """Refresh every configured ontology.

    Implements `RefreshKind`. One service serves every configured ontology; the
    job's source key selects the ontology's definition in `config/sources.yaml`.

    Args:
        sources: Configured sources, which say how to read each ontology.
    """

    def __init__(self, sources: RefreshSources) -> None:
        self._sources = sources

    @property
    def name(self) -> RefreshKindName:
        """Return `ontology`."""
        return RefreshKindName.ONTOLOGY

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `ontology_refresh`."""
        return frozenset({JobType.ONTOLOGY_REFRESH})

    def apply(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        job: Job,
        document: SourceDocument,
        report: ProgressReporter,
    ) -> RefreshOutcome:
        """Store and activate a new snapshot unless the document is already active.

        A document whose content the active snapshot already holds, read with the
        same reader version, identifier prefixes, and closure predicates, is not
        stored again. Either way, ontology snapshots that are no longer kept are
        pruned.

        Raises:
            TerminalRefreshError: With `invalid_document` if the OBO is invalid
                or does not declare every subset listed in the ontology's
                `required_subsets` setting.
        """
        definition = self._sources.ontology_definition(document.source_key)
        ontology_document = _ontology_document(definition.key, document)
        snapshots = unit_of_work.snapshots
        content = SourceContent.of(
            RefreshKindName.ONTOLOGY,
            document.provenance,
            identifier_prefixes=definition.identifier_prefixes,
            closure_predicates=definition.closure_predicates,
        )
        if snapshots.matching_active(SnapshotKind.ONTOLOGY, definition.key, content):
            snapshots.prune(SnapshotKind.ONTOLOGY)
            return _unchanged(ontology_document)
        snapshot = _parse(ontology_document, definition)
        report("storing")
        record = snapshots.create(
            SnapshotKind.ONTOLOGY, definition.key, job.job_id, document.provenance
        )
        closure_count = unit_of_work.ontologies.insert(
            record.snapshot_id, snapshot, compute_closure(snapshot)
        )
        report("activating")
        snapshots.activate(record)
        version = OntologyVersion(
            version_id=record.snapshot_id,
            ontology_key=definition.key,
            source_type=record.source_type,
            source_locator=record.source_locator,
            source_revision=record.source_revision,
            source_checksum=record.source_checksum,
            document_version=snapshot.document_version,
            loaded_predicates=tuple(snapshot.closure_predicates),
            fetched_at=record.fetched_at,
            term_count=len(snapshot.terms),
            closure_count=closure_count,
            active=True,
        )
        result = OntologyRefreshResult(
            version,
            ontology_warnings=ontology_refresh_warnings(
                snapshot.terms, definition.identifier_prefixes
            ),
        )
        AuditService(unit_of_work.audit).record_ontology_refreshed(
            actor_id=job.requested_by,
            job_id=job.job_id,
            details={
                "ontology_key": definition.key,
                "ontology_version_id": str(record.snapshot_id),
                "source_revision": record.source_revision,
                "term_count": version.term_count,
                "closure_count": version.closure_count,
                "ontology_warning_count": len(result.ontology_warnings),
            },
        )
        snapshots.prune(SnapshotKind.ONTOLOGY)
        # Queued last: the queue lock is held until this transaction commits.
        replacement = queue_or_join(
            unit_of_work,
            job_type=JobType.TERM_REPLACEMENT,
            group_key=None,
            requested_by=job.requested_by,
        )
        return replace(
            _stored(result.to_stored()),
            follow_ups=(replacement.job_id,),
        )


def _parse(
    document: OntologyDocument, definition: OntologyDefinition
) -> OntologySnapshot:
    """Parse an OBO document and require the subsets its definition lists.

    Raises:
        TerminalRefreshError: With `invalid_document` if the document cannot be
            parsed or lacks a required subset.
    """
    try:
        snapshot = parse_obo(document, definition)
    except OntologyParseError as error:
        # An invalid file stays invalid, so retrying cannot help.
        raise TerminalRefreshError(
            failure_code=JobFailureCode.INVALID_DOCUMENT,
            failure_details={"message": str(error)},
        ) from None
    missing = missing_required_subsets(snapshot, definition)
    if missing:
        raise TerminalRefreshError(
            failure_code=JobFailureCode.INVALID_DOCUMENT,
            failure_details={
                "message": "ontology source does not declare the subsets "
                + ", ".join(missing)
            },
        )
    return snapshot


def _ontology_document(key: str, document: SourceDocument) -> OntologyDocument:
    """Convert a fetched document into the ontology parser's input."""
    return OntologyDocument(
        ontology_key=key,
        content=document.content,
        source_type=document.source_type,
        source_locator=document.source_locator,
        source_revision=document.source_revision,
        source_checksum=document.source_checksum,
        fetched_at=document.fetched_at,
    )


def _unchanged(document: OntologyDocument) -> RefreshOutcome:
    """Build the outcome for a document that is already the active snapshot."""
    result = OntologyRefreshResult.for_active_document(document).to_stored()
    return replace(_stored(result), unchanged=True)


def _stored(stored: StoredOntologyRefresh) -> RefreshOutcome:
    """Build the job's outcome from a stored refresh result."""
    warning_count = len(stored.ontology_warnings)
    warnings: tuple[str, ...] = ()
    if warning_count:
        noun = "warning" if warning_count == 1 else "warnings"
        warnings = (f"Ontology refresh completed with {warning_count} ontology {noun}",)
    return RefreshOutcome(
        result=STORED_ONTOLOGY_REFRESH_RESULT.dump(stored),
        counts={"ontology_warning_count": warning_count},
        warnings=warnings,
    )
