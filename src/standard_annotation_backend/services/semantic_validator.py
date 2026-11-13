"""Check annotations against stored reference data before they are written."""

from uuid import UUID

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.ontology import OntologyCatalog
from standard_annotation_backend.domain.semantic_validation.rules import (
    validate_semantics,
)
from standard_annotation_backend.domain.validation import ValidationIssue
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork


class SemanticValidator:
    """Check structurally valid annotations against the active reference data.

    Structural validation checks an annotation against the Standard Annotation
    schema. This class runs the semantic checks, which need stored data: the
    annotation's entity must be in an active entity catalog, its terms must be
    current, it must not duplicate another active annotation, and it must pass
    the GO Rules. Every configured ontology is checked, so an identifier with
    one of an ontology's prefixes must be a current term of that ontology.

    Args:
        ontologies: The configured ontologies and the identifier prefixes each owns.
    """

    def __init__(self, ontologies: OntologyCatalog) -> None:
        self._ontologies = ontologies

    def issues(
        self,
        unit_of_work: SqlAlchemyUnitOfWork,
        annotation: Annotation,
        duplicate_peer_ids: tuple[UUID, ...],
    ) -> tuple[ValidationIssue, ...]:
        """Return every semantic issue of one annotation, always in the same order.

        Args:
            unit_of_work: The transaction to read reference data in.
            annotation: A structurally valid annotation.
            duplicate_peer_ids: IDs of active annotations that duplicate it. A
                write passes the IDs that `prepare_create` or `prepare_update`
                found while holding the locks that prevent concurrent duplicates.

        Raises:
            ReferenceDataUnavailableError: If a configured ontology or the go-site
                rule data has never been loaded, so it has no active snapshot.
        """
        (facts,) = unit_of_work.validation_facts.gather(
            [annotation], [duplicate_peer_ids], self._ontologies
        )
        return validate_semantics(annotation, facts)
