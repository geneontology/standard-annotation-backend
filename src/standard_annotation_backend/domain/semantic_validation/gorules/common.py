"""Share the helpers GO Rules use to read an annotation and its facts.

Every rule about the annotated term needs that term to be current. An unknown,
obsolete, or alternative `ontology_class_id` is already reported as its own issue,
so those rules report nothing for it. Once the term is fixed, the next attempt may
report issues from those rules.
"""

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.details import (
    IssueDetails,
)
from standard_annotation_backend.domain.semantic_validation.evidence import (
    go_evidence_code,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    AnnotationFacts,
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.validation import (
    ANNOTATION_ROOT,
    ValidationIssue,
)

type Location = tuple[str | int, ...]
"""Where an issue points, written the way validation issues locate values."""

WHOLE_ANNOTATION: Location = (ANNOTATION_ROOT,)
"""The location of an issue about a combination of fields."""


def gorule_issue(
    number: int,
    message: str,
    location: Location = WHOLE_ANNOTATION,
    details: IssueDetails | None = None,
) -> ValidationIssue:
    """Return an issue for one GO Rule.

    Args:
        number: The GO Rule number, such as 2 for GORULE:0000002.
        message: A short, stable explanation of the failure.
        location: Where the issue points; the whole annotation by default.
        details: Facts that help fix the issue, in the shape the rule declares.

    Returns:
        An issue whose type is `gorule_` and the seven-digit rule number.
    """
    issue = ValidationIssue(
        location=location, message=message, type=f"gorule_{number:07d}"
    )
    if details is not None:
        issue["details"] = details
    return issue


def evidence_code(annotation: Annotation) -> str:
    """Return the GO evidence code of the annotation's evidence term, such as `IPI`."""
    return go_evidence_code(annotation.evidence_type)


def annotated_term(annotation: Annotation, facts: AnnotationFacts) -> TermFact | None:
    """Return the fact about `ontology_class_id` when it is a current term."""
    fact = facts.terms.get(annotation.ontology_class_id)
    if fact is None or fact.status is not TermStatus.CURRENT:
        return None
    return fact
