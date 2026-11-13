"""Check an annotation's entity, terms, and duplicates against its facts.

Each check reports one issue type. The term checks look at every identifier that may
hold a term (see `term_identifiers`) and report them in field order. Identifiers
whose prefix no configured ontology owns have no fact and are not checked.
"""

from collections.abc import Iterator

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.details import (
    DuplicatePeersDetails,
    ObsoleteTermDetails,
    PrimaryIdDetails,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    AnnotationFacts,
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.semantic_validation.identifiers import (
    IdentifierOccurrence,
    term_identifiers,
)
from standard_annotation_backend.domain.validation import (
    ANNOTATION_ROOT,
    ValidationIssue,
)


def check_entity(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report a `db_object_id` that no entity catalog lists as active."""
    if facts.entity is None:
        yield ValidationIssue(
            location=(ANNOTATION_ROOT, "db_object_id"),
            message="db_object_id is not an active entity in any entity catalog",
            type="unknown_entity",
        )


def _terms_with_status(
    annotation: Annotation, facts: AnnotationFacts, status: TermStatus
) -> Iterator[tuple[IdentifierOccurrence, TermFact]]:
    """Yield each identifier whose fact has `status`, with the fact."""
    for occurrence in term_identifiers(annotation):
        fact = facts.terms.get(occurrence.identifier)
        if fact is not None and fact.status is status:
            yield occurrence, fact


def check_unknown_terms(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report each identifier that is not a term of the ontology owning its prefix."""
    for occurrence, _ in _terms_with_status(annotation, facts, TermStatus.UNKNOWN):
        yield ValidationIssue(
            location=occurrence.location,
            message="Identifier is not a term of the ontology that owns its prefix",
            type="unknown_term",
        )


def check_obsolete_terms(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report each obsolete term, with the terms suggested instead."""
    for occurrence, fact in _terms_with_status(annotation, facts, TermStatus.OBSOLETE):
        yield ValidationIssue(
            location=occurrence.location,
            message="Term is obsolete",
            type="obsolete_term",
            details=ObsoleteTermDetails(
                replaced_by=list(fact.replaced_by), consider=list(fact.consider)
            ),
        )


def check_alternate_term_ids(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report each alternative identifier, with its primary term."""
    for occurrence, fact in _terms_with_status(annotation, facts, TermStatus.ALTERNATE):
        issue = ValidationIssue(
            location=occurrence.location,
            message="Identifier is an alternative ID; use its primary term",
            type="alternate_term_id",
        )
        # Facts about alternative identifiers always name their primary term.
        if fact.primary_id is not None:
            issue["details"] = PrimaryIdDetails(primary_id=fact.primary_id)
        yield issue


def check_duplicates(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report the other active annotations that duplicate this one.

    The rules in `duplicate_policy` decide which annotations are duplicates. The
    issue lists every one of them, including annotations that already duplicated
    this one before the current write, so updating one of two existing duplicates
    is reported too.
    """
    if facts.duplicate_peer_ids:
        yield ValidationIssue(
            location=(ANNOTATION_ROOT,),
            message="Annotation duplicates other active annotations",
            type="duplicate_annotation",
            details=DuplicatePeersDetails(
                peer_ids=[str(peer) for peer in facts.duplicate_peer_ids]
            ),
        )
