"""Check an annotation against the facts read about it.

Each check takes an annotation and its facts and returns the issues it finds,
without reading anything else. `validate_semantics` runs every check in a fixed
order, so the same annotation and facts always give the same issues in the same
order. The entity, term, and duplicate checks come first, then the GO Rules in
rule-number order.
"""

from collections.abc import Callable, Iterable, Iterator

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.facts import (
    AnnotationFacts,
    TermStatus,
)
from standard_annotation_backend.domain.semantic_validation.gorules.metadata import (
    gorule_0000027,
    gorule_0000043,
    gorule_0000058,
)
from standard_annotation_backend.domain.semantic_validation.gorules.support import (
    gorule_0000016,
    gorule_0000017,
    gorule_0000018,
    gorule_0000022,
    gorule_0000030,
    gorule_0000033,
    gorule_0000037,
    gorule_0000050,
    gorule_0000055,
    gorule_0000063,
)
from standard_annotation_backend.domain.semantic_validation.gorules.terms import (
    gorule_0000002,
    gorule_0000005,
    gorule_0000006,
    gorule_0000007,
    gorule_0000008,
    gorule_0000011,
    gorule_0000015,
    gorule_0000039,
    gorule_0000042,
    gorule_0000046,
    gorule_0000053,
    gorule_0000061,
    gorule_0000065,
)
from standard_annotation_backend.domain.semantic_validation.identifiers import (
    term_identifiers,
)
from standard_annotation_backend.domain.validation import (
    ANNOTATION_ROOT,
    ValidationIssue,
)

type SemanticCheck = Callable[[Annotation, AnnotationFacts], Iterable[ValidationIssue]]
"""A function that returns the issues it finds in an annotation, given its facts."""


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


def check_terms(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report each identifier that is not a current term of the ontology owning it.

    Identifiers whose prefix no configured ontology owns have no fact and are not
    checked.
    """
    for occurrence in term_identifiers(annotation):
        fact = facts.terms.get(occurrence.identifier)
        if fact is None or fact.status is TermStatus.CURRENT:
            continue
        if fact.status is TermStatus.OBSOLETE:
            yield ValidationIssue(
                location=occurrence.location,
                message="Term is obsolete",
                type="obsolete_term",
                details={
                    "replaced_by": list(fact.replaced_by),
                    "consider": list(fact.consider),
                },
            )
        elif fact.status is TermStatus.ALTERNATE:
            yield ValidationIssue(
                location=occurrence.location,
                message="Identifier is an alternative ID; use its primary term",
                type="alternate_term_id",
                details={"primary_id": fact.primary_id},
            )
        else:
            yield ValidationIssue(
                location=occurrence.location,
                message="Identifier is not a term of the ontology that owns its prefix",
                type="unknown_term",
            )


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
            details={"peer_ids": [str(peer) for peer in facts.duplicate_peer_ids]},
        )


GO_RULES: tuple[SemanticCheck, ...] = (
    gorule_0000002,
    gorule_0000005,
    gorule_0000006,
    gorule_0000007,
    gorule_0000008,
    gorule_0000011,
    gorule_0000015,
    gorule_0000016,
    gorule_0000017,
    gorule_0000018,
    gorule_0000022,
    gorule_0000027,
    gorule_0000030,
    gorule_0000033,
    gorule_0000037,
    gorule_0000039,
    gorule_0000042,
    gorule_0000043,
    gorule_0000046,
    gorule_0000050,
    gorule_0000053,
    gorule_0000055,
    gorule_0000058,
    gorule_0000061,
    gorule_0000063,
    gorule_0000065,
)
"""Every write-blocking GO Rule, in rule-number order.

A write-blocking rule is one SABE can check using only one annotation and the
reference data, so SABE rejects any write that breaks it.
"""

SEMANTIC_CHECKS: tuple[SemanticCheck, ...] = (
    check_entity,
    check_terms,
    check_duplicates,
    *GO_RULES,
)
"""Every semantic check, in the order its issues are reported."""


def validate_semantics(
    annotation: Annotation, facts: AnnotationFacts
) -> tuple[ValidationIssue, ...]:
    """Run every semantic check and return all of their issues in order."""
    return tuple(
        issue for check in SEMANTIC_CHECKS for issue in check(annotation, facts)
    )
