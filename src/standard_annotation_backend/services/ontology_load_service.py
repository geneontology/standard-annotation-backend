"""Stage ontology snapshots and update annotations during activation."""

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.ontology import (
    AnnotationReplacementProposal,
    OntologyDefinition,
    OntologyDocument,
    OntologyFinding,
    OntologyKey,
    OntologyLoadResult,
    OntologySnapshot,
    OntologyTerm,
    OntologyVersion,
    compute_closure,
    ontology_load_warnings,
    propose_term_replacements,
)
from standard_annotation_backend.persistence.annotation_data import (
    AnnotationPersistenceData,
    prepare_annotation_for_persistence,
)
from standard_annotation_backend.persistence.models import (
    OntologyMetadataRecord,
    OntologyTermRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.audit_service import AuditService


class OntologyCandidateConflictError(RuntimeError):
    """Report that a staged job conflicts with its source or activation order."""


class OntologyLoadService:
    """Coordinate staging, activation, and annotation updates for ontology loads."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        definition: OntologyDefinition,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._definition = definition

    def matches_active(self, document: OntologyDocument) -> bool:
        """Return whether the document's source details match the active snapshot."""
        with self._unit_of_work_factory() as unit_of_work:
            return unit_of_work.ontologies.matches_active_source(document)

    def completed(self, job_id: UUID) -> dict[str, object] | None:
        """Return the stored result if this job completed ontology activation."""
        with self._unit_of_work_factory() as unit_of_work:
            record = unit_of_work.ontologies.get_by_job(job_id)
            if record is None:
                return None
            if record.load_result is not None:
                return dict(record.load_result)
            if record.active:
                raise RuntimeError("active ontology snapshot has no durable result")
            return None

    def prune(self, *, pruned_at: datetime) -> tuple[UUID, ...]:
        """Delete eligible snapshot term and closure rows and commit the transaction.

        Args:
            pruned_at: Time to record on each snapshot that is pruned.

        Returns:
            Version identifiers for snapshots pruned by this call.
        """
        with self._unit_of_work_factory() as unit_of_work:
            pruned = unit_of_work.ontologies.prune_candidates(
                self._definition.key, pruned_at
            )
            unit_of_work.commit()
        return pruned

    def stage(
        self,
        *,
        job_id: UUID,
        document: OntologyDocument,
        snapshot: OntologySnapshot,
    ) -> OntologyVersion:
        """Persist and commit one complete inactive candidate snapshot."""
        with self._unit_of_work_factory() as unit_of_work:
            existing = unit_of_work.ontologies.get_by_job(job_id)
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
                return _ontology_version(
                    existing,
                    term_count=unit_of_work.ontologies.term_count(existing.version_id),
                    closure_count=unit_of_work.ontologies.closure_count(
                        existing.version_id
                    ),
                )
            record = unit_of_work.ontologies.stage(
                job_id=job_id,
                document=document,
                snapshot=snapshot,
                closure_rows=compute_closure(snapshot),
            )
            version = _ontology_version(
                record,
                term_count=len(snapshot.terms),
                closure_count=unit_of_work.ontologies.closure_count(record.version_id),
            )
            unit_of_work.commit()
        return version

    def activate(self, *, job_id: UUID, actor_id: str) -> OntologyLoadResult:
        """Activate a snapshot and apply replacements that do not create duplicates."""
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
            warnings = ontology_load_warnings(candidate_terms)
            if candidate.active:
                return OntologyLoadResult(
                    True,
                    version,
                    0,
                    0,
                    0,
                    (),
                    ontology_warnings=warnings,
                )
            if (
                previous is not None
                and previous.staging_sequence > candidate.staging_sequence
            ):
                raise OntologyCandidateConflictError(
                    "a newer ontology snapshot is already active"
                )

            previous_terms = (
                {}
                if previous is None
                else _terms(unit_of_work.ontologies.list_terms(previous.version_id))
            )
            snapshot = _snapshot(candidate, candidate_terms)
            records = unit_of_work.annotations.lock_all_active_for_system_update()
            before = {
                record.annotation_id: prepare_annotation_for_persistence(
                    Annotation.model_validate(record.annotation_data)
                )
                for record in records
            }
            proposals = {
                record.annotation_id: propose_term_replacements(
                    Annotation.model_validate(record.annotation_data),
                    snapshot,
                    previous_terms,
                    self._definition,
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
            proposed_data = {
                annotation_id: prepare_annotation_for_persistence(proposal.annotation)
                for annotation_id, proposal in proposals.items()
                if proposal.changed_paths
            }
            before_pairs = _duplicate_pairs(before)
            rejected: set[UUID] = set()
            while True:
                after = {
                    annotation_id: (
                        proposed_data[annotation_id]
                        if annotation_id in accepted
                        else persistence_data
                    )
                    for annotation_id, persistence_data in before.items()
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
                            self._definition.key,
                        )
                    )
                accepted.difference_update(newly_rejected)
                rejected.update(newly_rejected)

            records_by_id = {record.annotation_id: record for record in records}
            audit = AuditService(unit_of_work.audit)
            for annotation_id in sorted(accepted):
                updated = unit_of_work.annotations.apply_system_update(
                    records_by_id[annotation_id],
                    proposed_data[annotation_id],
                    actor_id=actor_id,
                    change_source="ontology_load",
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
            result = OntologyLoadResult(
                applied=True,
                ontology_version=version,
                annotation_scan_count=len(records),
                annotation_update_count=len(accepted),
                annotation_skip_count=len(skipped_ids - accepted),
                findings=tuple(findings),
                ontology_warnings=warnings,
            )
            unit_of_work.ontologies.record_load_result(
                candidate, result.to_job_result()
            )
            audit.record_ontology_loaded(
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
        retrieved_at=record.loaded_at,
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
        loaded_at=record.loaded_at,
        term_count=term_count,
        closure_count=closure_count,
        active=record.active,
    )


def _duplicate_pairs(
    annotations: dict[UUID, AnnotationPersistenceData],
) -> set[tuple[UUID, UUID]]:
    """Return all duplicate annotation ID pairs in the supplied candidate state."""
    indexed: dict[str, dict[str, list[UUID]]] = {}
    for annotation_id, annotation in annotations.items():
        references = indexed.setdefault(annotation.duplicate_base_signature, {})
        for reference in set(annotation.canonical_references):
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
