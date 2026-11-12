"""Run semantic validation for the services that write annotations."""

from uuid import UUID

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.refresh import RefreshKindName
from standard_annotation_backend.domain.semantic_validation.rules import (
    validate_semantics,
)
from standard_annotation_backend.domain.validation import ValidationIssue
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork
from standard_annotation_backend.refresh.sources import RefreshSources


class SemanticValidator:
    """Check structurally valid annotations against the active reference data.

    Every configured ontology is enforced: an identifier whose prefix one owns must
    be a current term of it.

    Args:
        sources: Configured sources, which name the ontologies and their prefixes.
    """

    def __init__(self, sources: RefreshSources) -> None:
        self._ontologies = {
            key: sources.ontology_definition(key).identifier_prefixes
            for key in sources.keys(RefreshKindName.ONTOLOGY)
        }

    def issues(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        annotation: Annotation,
        duplicate_peer_ids: tuple[UUID, ...],
    ) -> tuple[ValidationIssue, ...]:
        """Return every semantic issue of one annotation, in a stable order.

        Args:
            unit_of_work: The transaction to read reference data in.
            annotation: A structurally valid annotation.
            duplicate_peer_ids: Active annotations that duplicate it. A write
                passes the peers its prepare step found under locks.

        Raises:
            ReferenceDataUnavailableError: If a configured ontology or the go-site
                rule data has no active snapshot.
        """
        (facts,) = unit_of_work.validation_facts.gather(
            [annotation], [duplicate_peer_ids], self._ontologies
        )
        return validate_semantics(annotation, facts)
