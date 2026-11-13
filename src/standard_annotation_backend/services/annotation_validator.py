"""Validate annotations before SABE writes them or reports on them."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from standard_annotation_backend.domain.annotations import (
    Annotation,
    InvalidAnnotationPayloadError,
)
from standard_annotation_backend.domain.duplicate_policy import duplicate_key
from standard_annotation_backend.domain.ontology import OntologyCatalog
from standard_annotation_backend.domain.semantic_validation.facts import (
    ActiveSnapshots,
)
from standard_annotation_backend.domain.semantic_validation.rules import (
    validate_semantics,
)
from standard_annotation_backend.domain.validation import (
    ValidationIssue,
    validate_annotation,
)
from standard_annotation_backend.persistence.repositories.annotations import (
    PeerQuery,
    ValidatedCreate,
    ValidatedUpdate,
)
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork


@dataclass(frozen=True, slots=True)
class Candidate:
    """An annotation to check without writing it.

    Attributes:
        payload: The annotation's data, or an `Annotation` that already passed
            the structural check.
        annotation_id: The stored annotation the candidate is, or would replace.
            It is never counted as the candidate's duplicate. `None` for a new
            annotation.
    """

    payload: object
    annotation_id: UUID | None = None


class AnnotationValidator:
    """Run every check an annotation must pass, in one fixed order.

    The structural check validates an annotation against the Standard Annotation
    schema. The semantic checks need stored data: the annotation's entity must be
    in an active entity catalog, its terms must be current, it must not duplicate
    another active annotation, and it must pass the GO Rules. Every configured
    ontology is checked, so an identifier with one of an ontology's prefixes must
    be a current term of that ontology. An annotation that fails the structural
    check is not checked further, so it gets only structural issues.

    Writes use `prepare_create` and `prepare_update`. They check the annotation
    while holding the locks a write needs and return the only values that
    `AnnotationRepository.create` and `update` accept, so no write skips a check.

    `check` validates many annotations without locking anything. Its answer is
    advisory: a concurrent write can change it. Change-set previews and QC
    reports use it.

    Args:
        ontologies: The configured ontologies and the identifier prefixes each owns.
    """

    def __init__(self, ontologies: OntologyCatalog) -> None:
        self._ontologies = ontologies

    def prepare_create(
        self, unit_of_work: SqlAlchemyUnitOfWork, payload: object
    ) -> ValidatedCreate:
        """Lock a new annotation and check it, for `AnnotationRepository.create`.

        Args:
            unit_of_work: The transaction that will write the annotation.
            payload: The annotation's data, or an `Annotation`.

        Raises:
            InvalidAnnotationPayloadError: If the annotation fails the structural
                check, or passes it and fails a semantic check. The error lists
                every issue the failing stage found.
            ReferenceDataUnavailableError: If a configured ontology or the go-site
                rule data has never been loaded, so it has no active snapshot.
        """
        annotation = _structurally_valid(payload)
        locked = unit_of_work.annotations.lock_for_create(annotation)
        self._require_no_semantic_issues(
            unit_of_work, annotation, locked.duplicate_peer_ids
        )
        return ValidatedCreate(locked)

    def prepare_update(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        annotation_id: UUID,
        payload: object,
        *,
        expected_version: int,
    ) -> ValidatedUpdate:
        """Lock an annotation and check its replacement, for `update`.

        The expected version is checked after the annotation is locked and
        before the semantic checks run.

        Args:
            unit_of_work: The transaction that will write the replacement.
            annotation_id: The annotation to replace.
            payload: The replacement's data, or an `Annotation`.
            expected_version: The version that must still be current.

        Raises:
            InvalidAnnotationPayloadError: If the replacement fails the structural
                check, or passes it and fails a semantic check.
            AnnotationNotFoundError: If the annotation does not exist.
            AnnotationDeletedError: If the annotation is deleted.
            StaleAnnotationVersionError: If `expected_version` is not current.
            ReferenceDataUnavailableError: If reference data needed for
                validation has never been loaded.
        """
        annotation = _structurally_valid(payload)
        locked = unit_of_work.annotations.lock_for_update(
            annotation_id, annotation, expected_version=expected_version
        )
        self._require_no_semantic_issues(
            unit_of_work, annotation, locked.duplicate_peer_ids
        )
        return ValidatedUpdate(locked)

    def check(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        candidates: Sequence[Candidate],
        *,
        snapshots: ActiveSnapshots | None = None,
    ) -> tuple[tuple[ValidationIssue, ...], ...]:
        """Return every issue of each candidate, in candidate order.

        Duplicate peers and facts are read in batches, so one call can check one
        annotation or thousands. Reference data is read only when some
        candidate passes the structural check.

        Args:
            unit_of_work: The transaction to read annotations and reference data in.
            candidates: The annotations to check.
            snapshots: Active snapshots the caller already looked up, for example
                a QC report that records which ones it used. Looked up here when
                omitted.

        Returns:
            For each candidate, its issues; an empty tuple means none.

        Raises:
            ReferenceDataUnavailableError: If a configured ontology or the go-site
                rule data has never been loaded, so it has no active snapshot.
        """
        results: list[tuple[ValidationIssue, ...]] = []
        valid: list[tuple[int, Annotation, UUID | None]] = []
        for index, candidate in enumerate(candidates):
            validation = validate_annotation(candidate.payload)
            if validation.annotation is None:
                results.append(validation.errors)
            else:
                results.append(())
                valid.append((index, validation.annotation, candidate.annotation_id))
        if not valid:
            return tuple(results)
        peers = unit_of_work.annotations.duplicate_peers(
            [
                PeerQuery(duplicate_key(annotation), annotation_id)
                for _, annotation, annotation_id in valid
            ]
        )
        facts = unit_of_work.validation_facts.gather(
            [annotation for _, annotation, _ in valid],
            peers,
            self._ontologies,
            snapshots=snapshots,
        )
        for (index, annotation, _), annotation_facts in zip(valid, facts, strict=True):
            results[index] = validate_semantics(annotation, annotation_facts)
        return tuple(results)

    def _require_no_semantic_issues(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        annotation: Annotation,
        duplicate_peer_ids: tuple[UUID, ...],
    ) -> None:
        """Raise `InvalidAnnotationPayloadError` if any semantic check fails."""
        (facts,) = unit_of_work.validation_facts.gather(
            [annotation], [duplicate_peer_ids], self._ontologies
        )
        issues = validate_semantics(annotation, facts)
        if issues:
            raise InvalidAnnotationPayloadError(issues)


def _structurally_valid(payload: object) -> Annotation:
    """Return the payload as an `Annotation`, or raise its structural issues."""
    validation = validate_annotation(payload)
    if validation.annotation is None:
        raise InvalidAnnotationPayloadError(validation.errors)
    return validation.annotation
