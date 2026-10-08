"""Refresh ontologies from configured OBO sources and prune old snapshots.

A refresh parses the OBO file, stages a complete inactive snapshot, and activates
it while applying safe term replacements to annotations. Only one process at a
time may stage, activate, or prune snapshots of one ontology. Each ontology has
a PostgreSQL advisory lock that a process takes only if it is free. A refresh
that finds it held raises `OntologyRefreshBusyError`, and Celery retries the task
later instead of waiting.
"""

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from standard_annotation_backend.domain.annotations import Annotation, ChangeSource
from standard_annotation_backend.domain.duplicate_policy import (
    DuplicateKey,
    duplicate_key,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.domain.ontology import (
    STORED_ONTOLOGY_REFRESH_RESULT,
    AnnotationReplacementProposal,
    OntologyDefinition,
    OntologyDocument,
    OntologyFinding,
    OntologyKey,
    OntologyParseError,
    OntologyRefreshResult,
    OntologySnapshot,
    OntologyTerm,
    OntologyVersion,
    StoredOntologyRefresh,
    compute_closure,
    ontology_refresh_warnings,
    propose_term_replacements,
)
from standard_annotation_backend.domain.refresh import (
    ProgressReporter,
    RefreshFailureCode,
    RefreshKindName,
    RefreshOutcome,
    RetryableRefreshError,
    SourceDocument,
    TerminalRefreshError,
    job_source_key,
)
from standard_annotation_backend.ontology.definitions import ontology_definition
from standard_annotation_backend.ontology.obo_parser import parse_obo
from standard_annotation_backend.persistence.locks import (
    AdvisoryTryLock,
    LockNamespace,
)
from standard_annotation_backend.persistence.models import (
    OntologyMetadataRecord,
    OntologyTermRecord,
)
from standard_annotation_backend.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    UnitOfWorkFactory,
)
from standard_annotation_backend.services.audit_service import AuditService
from standard_annotation_backend.services.job_service import Job

logger = logging.getLogger(__name__)


class OntologyRefreshBusyError(RetryableRefreshError):
    """Report that another process holds the ontology's refresh lock."""


class OntologyRefreshService:
    """Refresh and prune every supported ontology.

    Implements `RefreshKind`. One service serves every ontology key; the job's
    source key selects the ontology's identifier prefixes and closure
    predicates.

    Args:
        unit_of_work_factory: Opens the transactions for staging, activation,
            and pruning.
        try_lock: Takes the per-ontology lock.
        enqueue_prune: Schedules pruning after a job finishes. `None` prunes in
            this process instead.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        try_lock: AdvisoryTryLock,
        enqueue_prune: Callable[[str], None] | None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._try_lock = try_lock
        self._enqueue_prune = enqueue_prune

    @property
    def name(self) -> RefreshKindName:
        """Return `ontology`."""
        return RefreshKindName.ONTOLOGY

    @property
    def job_types(self) -> frozenset[JobType]:
        """Return `ontology_refresh`."""
        return frozenset({JobType.ONTOLOGY_REFRESH})

    def recover(self, job: Job) -> RefreshOutcome | None:
        """Return the stored result if this job already activated its snapshot.

        A key that is not a supported ontology has nothing to recover; the runner
        then reports it as an unknown source.

        Raises:
            StoredDataError: If the stored result is malformed; the run is retried.
        """
        key = _ontology_key(job_source_key(job.parameters))
        if key is None:
            return None
        # Activation commits the snapshot and its result together, so a stored
        # result means an earlier attempt stopped only before marking the job
        # succeeded.
        completed = self._completed(job.job_id)
        return None if completed is None else _stored(completed)

    def apply(
        self, job: Job, document: SourceDocument, report: ProgressReporter
    ) -> RefreshOutcome:
        """Parse, stage, and activate a new snapshot under the ontology's lock.

        A document that is already the active snapshot is not applied. It is
        checked before taking the lock, so it never waits on another job, and
        again once the lock is held.

        Raises:
            OntologyRefreshBusyError: If another process holds the lock. This is
                not a terminal failure, so the run is retried later.
            TerminalRefreshError: With `invalid_document` if the OBO is invalid.
            OntologyCandidateConflictError: If the job already staged a different
                source, or a snapshot staged after this job's is already active.
        """
        key = OntologyKey(document.source_key)
        definition = ontology_definition(key)
        ontology_document = _ontology_document(key, document)
        if self._matches_active(ontology_document):
            return _unchanged(ontology_document)
        # Stage and activate under the per-ontology lock, so two jobs cannot
        # activate snapshots out of order and pruning cannot delete a candidate.
        with self._try_lock(LockNamespace.ONTOLOGY, key.value) as acquired:
            if not acquired:
                # The job stays running; Celery retries the whole task later.
                raise OntologyRefreshBusyError
            # Another job may have activated this same document while this one
            # was fetching, so check again now that the lock is held.
            if self._matches_active(ontology_document):
                return _unchanged(ontology_document)
            try:
                snapshot = parse_obo(ontology_document, definition)
            except OntologyParseError as error:
                # An invalid file stays invalid, so retrying cannot help.
                raise TerminalRefreshError(
                    failure_code=RefreshFailureCode.INVALID_DOCUMENT,
                    failure_details={"message": str(error)},
                ) from None
            self._stage(
                job_id=job.job_id, document=ontology_document, snapshot=snapshot
            )
            result = self._activate(
                definition, job_id=job.job_id, actor_id=job.requested_by
            )
        return _stored(result.to_stored())

    def discard_staging(self, uow: SqlAlchemyUnitOfWork, job_id: UUID) -> None:
        """Ontology candidates are removed by pruning, not on failure."""

    def after_terminal(self, source_key: str) -> None:
        """Prune old snapshot data now that this ontology's job has finished.

        Celery workers enqueue `sab.ontology.prune`, which retries while the lock
        is busy. The CLI prunes in its own process and skips pruning when the
        lock is busy; the next finished job prunes instead.
        """
        if _ontology_key(source_key) is None:
            return
        if self._enqueue_prune is not None:
            self._enqueue_prune(source_key)
            return
        try:
            if not self.prune(source_key):
                logger.info(
                    "Ontology pruning skipped because the ontology is busy: "
                    "ontology_key=%s",
                    source_key,
                )
        except Exception as error:
            # The job already finished and committed its result, so a pruning
            # failure must not make the caller report the refresh as failed.
            # Pruning is only cleanup: the next finished ontology job prunes
            # again and removes whatever this attempt left behind. Log the
            # exception type only, since details could expose infrastructure.
            logger.error(
                "Ontology pruning failed: ontology_key=%s failure_type=%s",
                source_key,
                type(error).__name__,
            )

    def prune(self, source_key: str) -> bool:
        """Delete unneeded term and closure rows for one ontology.

        Returns:
            `False` if another process holds the ontology's lock, otherwise `True`.
            An unsupported key has nothing to prune and also returns `True`.
        """
        key = _ontology_key(source_key)
        if key is None:
            return True
        with self._try_lock(LockNamespace.ONTOLOGY, key.value) as acquired:
            if not acquired:
                return False
            self._prune(key, pruned_at=datetime.now(UTC))
        return True

    def _matches_active(self, document: OntologyDocument) -> bool:
        """Return whether the document's source details match the active snapshot."""
        with self._unit_of_work_factory() as unit_of_work:
            return unit_of_work.ontologies.matches_active_source(document)

    def _completed(self, job_id: UUID) -> StoredOntologyRefresh | None:
        """Return the stored result if this job completed ontology activation.

        Raises:
            RuntimeError: If the job's snapshot is active but has no stored result.
            StoredDataError: If the stored result does not have its expected shape.
        """
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.ontologies.get_by_job(job_id)
            if record is None:
                return None
            if record.refresh_result is not None:
                return STORED_ONTOLOGY_REFRESH_RESULT.load(record.refresh_result)
            if record.active:
                raise RuntimeError("active ontology snapshot has no durable result")
            return None

    def _prune(self, key: OntologyKey, *, pruned_at: datetime) -> tuple[UUID, ...]:
        """Delete eligible snapshot term and closure rows and commit the transaction.

        Args:
            key: Ontology whose snapshots are pruned.
            pruned_at: Time to record on each snapshot that is pruned.

        Returns:
            Version identifiers for snapshots pruned by this call.
        """
        with self._unit_of_work_factory() as unit_of_work:
            pruned = unit_of_work.ontologies.prune_candidates(key, pruned_at)
            unit_of_work.commit()
        return pruned

    def _stage(
        self,
        *,
        job_id: UUID,
        document: OntologyDocument,
        snapshot: OntologySnapshot,
    ) -> None:
        """Persist and commit one complete inactive candidate snapshot.

        If the job already staged the same source, its existing candidate is kept.

        Raises:
            OntologyCandidateConflictError: If the job already staged a different
                source.
            OntologySnapshotPrunedError: If the job's existing candidate was
                pruned.
        """
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.ontologies.stage(
                job_id=job_id,
                document=document,
                snapshot=snapshot,
                closure_rows=compute_closure(snapshot),
            )
            # Reading the counts raises if a kept candidate was already pruned.
            unit_of_work.ontologies.term_count(record.version_id)
            unit_of_work.ontologies.closure_count(record.version_id)
            unit_of_work.commit()

    def _activate(
        self, definition: OntologyDefinition, *, job_id: UUID, actor_id: str
    ) -> OntologyRefreshResult:
        """Activate a snapshot and apply replacements that do not create duplicates.

        Activation, annotation updates, audit events, and the stored result are
        committed together.

        Raises:
            OntologyCandidateConflictError: If a snapshot staged after this job's
                candidate is already active.
        """
        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.annotations.acquire_exclusive_system_update_lock()
            candidate, previous = unit_of_work.ontologies.lock_activation_state(job_id)
            candidate_terms = _terms(
                unit_of_work.ontologies.list_terms(candidate.version_id)
            )
            candidate_closure_count = unit_of_work.ontologies.closure_count(
                candidate.version_id
            )
            version = _ontology_version(
                candidate,
                term_count=len(candidate_terms),
                closure_count=candidate_closure_count,
            )
            warnings = ontology_refresh_warnings(candidate_terms)
            if candidate.active:
                return OntologyRefreshResult(
                    version,
                    0,
                    0,
                    0,
                    (),
                    ontology_warnings=warnings,
                )
            previous_terms = (
                {}
                if previous is None
                else _terms(unit_of_work.ontologies.list_terms(previous.version_id))
            )
            snapshot = _snapshot(candidate, candidate_terms)
            records = unit_of_work.annotations.lock_all_active_for_system_update()
            before = {
                record.annotation_id: duplicate_key(
                    Annotation.model_validate(record.annotation_data)
                )
                for record in records
            }
            proposals = {
                record.annotation_id: propose_term_replacements(
                    Annotation.model_validate(record.annotation_data),
                    snapshot,
                    previous_terms,
                    definition,
                )
                for record in records
            }
            findings = [
                replace(finding, annotation_id=annotation_id)
                for annotation_id, proposal in proposals.items()
                for finding in proposal.findings
            ]
            accepted = {
                annotation_id
                for annotation_id, proposal in proposals.items()
                if proposal.changed_paths
            }
            proposed_keys = {
                annotation_id: duplicate_key(proposal.annotation)
                for annotation_id, proposal in proposals.items()
                if proposal.changed_paths
            }
            before_pairs = _duplicate_pairs(before)
            rejected: set[UUID] = set()
            while True:
                after = {
                    annotation_id: (
                        proposed_keys[annotation_id]
                        if annotation_id in accepted
                        else key
                    )
                    for annotation_id, key in before.items()
                }
                introduced = _duplicate_pairs(after) - before_pairs
                newly_rejected = {
                    annotation_id
                    for pair in introduced
                    for annotation_id in pair
                    if annotation_id in accepted
                }
                if not newly_rejected:
                    break
                for annotation_id in sorted(newly_rejected):
                    peers = tuple(
                        sorted(
                            peer
                            for pair in introduced
                            if annotation_id in pair
                            for peer in pair
                            if peer != annotation_id
                        )
                    )
                    findings.extend(
                        _duplicate_findings(
                            annotation_id,
                            proposals[annotation_id],
                            peers,
                            definition.key,
                        )
                    )
                accepted.difference_update(newly_rejected)
                rejected.update(newly_rejected)

            records_by_id = {record.annotation_id: record for record in records}
            audit = AuditService(unit_of_work.audit)
            for annotation_id in sorted(accepted):
                updated = unit_of_work.annotations.apply_system_update(
                    records_by_id[annotation_id],
                    proposals[annotation_id].annotation,
                    actor_id=actor_id,
                    change_source=ChangeSource.ONTOLOGY_REFRESH,
                )
                audit.record_ontology_annotation_update(
                    actor_id=actor_id,
                    job_id=job_id,
                    annotation_id=annotation_id,
                    annotation_version=updated.current_version,
                )

            activated = unit_of_work.ontologies.activate(candidate.version_id)
            version = replace(version, active=activated.active)
            skipped_ids = rejected | {
                finding.annotation_id
                for finding in findings
                if finding.annotation_id is not None
            }
            result = OntologyRefreshResult(
                ontology_version=version,
                annotation_scan_count=len(records),
                annotation_update_count=len(accepted),
                annotation_skip_count=len(skipped_ids - accepted),
                findings=tuple(findings),
                ontology_warnings=warnings,
            )
            unit_of_work.ontologies.record_refresh_result(
                candidate, result.to_job_result()
            )
            audit.record_ontology_refreshed(
                actor_id=actor_id,
                job_id=job_id,
                details={
                    "ontology_key": candidate.ontology_key,
                    "ontology_version_id": str(candidate.version_id),
                    "source_revision": candidate.source_revision,
                    "term_count": version.term_count,
                    "closure_count": version.closure_count,
                    "annotation_scan_count": result.annotation_scan_count,
                    "annotation_update_count": result.annotation_update_count,
                    "annotation_skip_count": result.annotation_skip_count,
                    "ontology_warning_count": len(result.ontology_warnings),
                },
            )
            unit_of_work.commit()
        return result


def _snapshot(
    record: OntologyMetadataRecord,
    terms: dict[str, OntologyTerm],
) -> OntologySnapshot:
    """Build the ontology snapshot needed to evaluate annotation replacements."""
    document = OntologyDocument(
        ontology_key=OntologyKey(record.ontology_key),
        content=b"",
        source_type=record.source_type,
        source_locator=record.source_locator,
        source_revision=record.source_revision,
        source_checksum=record.source_checksum,
        fetched_at=record.fetched_at,
    )
    return OntologySnapshot(
        document=document,
        document_version=record.document_version,
        closure_predicates=tuple(record.loaded_predicates),
        terms=terms,
        edges=(),
    )


def _terms(records: list[OntologyTermRecord]) -> dict[str, OntologyTerm]:
    """Convert stored term rows to ontology terms indexed by identifier."""
    return {
        record.term_id: OntologyTerm(
            record.term_id,
            record.obsolete,
            tuple(record.replaced_by),
            tuple(record.consider),
        )
        for record in records
    }


def _ontology_version(
    record: OntologyMetadataRecord,
    *,
    term_count: int,
    closure_count: int,
) -> OntologyVersion:
    """Build an ontology-version result from stored metadata and row counts."""
    return OntologyVersion(
        version_id=record.version_id,
        ontology_key=OntologyKey(record.ontology_key),
        source_type=record.source_type,
        source_locator=record.source_locator,
        source_revision=record.source_revision,
        source_checksum=record.source_checksum,
        document_version=record.document_version,
        loaded_predicates=tuple(record.loaded_predicates),
        fetched_at=record.fetched_at,
        term_count=term_count,
        closure_count=closure_count,
        active=record.active,
    )


def _duplicate_pairs(
    keys: dict[UUID, DuplicateKey],
) -> set[tuple[UUID, UUID]]:
    """Return all duplicate annotation ID pairs in the supplied candidate state."""
    indexed: dict[str, dict[str, list[UUID]]] = {}
    for annotation_id, key in keys.items():
        references = indexed.setdefault(key.signature, {})
        for reference in set(key.references):
            references.setdefault(reference, []).append(annotation_id)

    pairs: set[tuple[UUID, UUID]] = set()
    for references in indexed.values():
        for identifiers in references.values():
            ordered = sorted(identifiers)
            pairs.update(
                (first_id, second_id)
                for index, first_id in enumerate(ordered)
                for second_id in ordered[index + 1 :]
            )
    return pairs


def _duplicate_findings(
    annotation_id: UUID,
    proposal: AnnotationReplacementProposal,
    peer_ids: tuple[UUID, ...],
    ontology_key: OntologyKey,
) -> tuple[OntologyFinding, ...]:
    """Describe replacements rejected because they would create duplicates."""
    return tuple(
        OntologyFinding(
            code="duplicate_conflict",
            ontology_key=ontology_key,
            term_id=replacement.term_id,
            field_paths=replacement.field_paths,
            replacement_ids=(replacement.replacement_id,),
            annotation_id=annotation_id,
            conflicting_annotation_ids=peer_ids,
        )
        for replacement in proposal.replacements
    )


def _ontology_key(source_key: str) -> OntologyKey | None:
    """Return the ontology for a source key, or `None` if it is not supported."""
    try:
        return OntologyKey(source_key)
    except ValueError:
        return None


def _ontology_document(key: OntologyKey, document: SourceDocument) -> OntologyDocument:
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
    finding_count = len(stored.findings)
    warning_count = len(stored.ontology_warnings)
    warnings: list[str] = []
    if warning_count:
        noun = "warning" if warning_count == 1 else "warnings"
        warnings.append(
            f"Ontology refresh completed with {warning_count} ontology {noun}"
        )
    if finding_count:
        warnings.append(f"Ontology refresh completed with {finding_count} findings")
    return RefreshOutcome(
        result=STORED_ONTOLOGY_REFRESH_RESULT.dump(stored),
        counts={
            "annotation_scan_count": stored.annotation_scan_count,
            "annotation_update_count": stored.annotation_update_count,
            "annotation_skip_count": stored.annotation_skip_count,
            "finding_count": finding_count,
            "ontology_warning_count": warning_count,
        },
        warnings=tuple(warnings),
    )
