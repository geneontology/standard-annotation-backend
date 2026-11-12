"""Check the GO Rules that read the go-site metadata.

The patterns from go-site (`id_syntax`, `allowed_reference`, and
`reference matches`) are matched against whole identifiers with
`re.fullmatch`. They were checked to compile when the files were refreshed.
"""

import re
from collections import Counter
from collections.abc import Iterator

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    PROTEIN_CONTAINING_COMPLEX,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    AnnotationFacts,
)
from standard_annotation_backend.domain.semantic_validation.gorules.common import (
    annotated_term,
    gorule_issue,
)
from standard_annotation_backend.domain.semantic_validation.identifiers import (
    database_identifiers,
    split_identifier,
)
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    EcoUsageConstraint,
    EntityIsComplex,
    WithPresence,
)
from standard_annotation_backend.domain.validation import (
    ANNOTATION_ROOT,
    ValidationIssue,
)


def gorule_0000027(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report identifiers or groups that the go-site metadata does not know.

    Every prefix in `db_object_id`, `with_or_from`, and extension terms must
    exactly match (case-sensitive) a `database` in `db-xrefs.yaml`; synonyms
    are not accepted. If that database defines any `id_syntax`, the local
    identifier must fully match at least one. `assigned_by` must be a
    `shorthand` in `groups.yaml`.
    """
    data = facts.rule_data
    if data is None:
        return
    databases = {entry.database: entry for entry in data.databases}
    for occurrence in database_identifiers(annotation):
        entry = databases.get(occurrence.prefix)
        if entry is None:
            yield gorule_issue(
                27, "Prefix is not a database in db-xrefs.yaml", occurrence.location
            )
        elif entry.id_syntaxes and not any(
            re.fullmatch(syntax, occurrence.local_id) for syntax in entry.id_syntaxes
        ):
            yield gorule_issue(
                27,
                "Local identifier does not match the database's identifier syntax",
                occurrence.location,
            )
    if annotation.assigned_by not in data.group_shorthands:
        yield gorule_issue(
            27,
            "assigned_by is not a group shorthand in groups.yaml",
            (ANNOTATION_ROOT, "assigned_by"),
        )


def gorule_0000043(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report a violation of the evidence term's `eco-usage-constraints.yaml` entry.

    When the entry sets `allowed_reference`, at least one reference must match
    it in full. Its `with_presence` decides whether `with_or_from` must be
    non-empty (`mandatory`) or empty (`disallowed`); a `conditional` entry
    chooses one from its condition. The complex condition cannot be evaluated
    for an unknown entity, so its requirement is then not checked.
    """
    data = facts.rule_data
    if data is None:
        return
    constraint = next(
        (
            entry
            for entry in data.eco_usage_constraints
            if entry.eco_id == annotation.evidence_type
        ),
        None,
    )
    if constraint is None:
        return
    if constraint.allowed_reference is not None and not any(
        re.fullmatch(constraint.allowed_reference, reference)
        for reference in annotation.references
    ):
        yield gorule_issue(
            43, "No reference is one of the kinds allowed for this evidence"
        )
    requirement = _with_requirement(constraint, annotation, facts)
    if requirement is WithPresence.MANDATORY and not annotation.with_or_from:
        yield gorule_issue(43, "This evidence requires with_or_from values")
    elif requirement is WithPresence.DISALLOWED and annotation.with_or_from:
        yield gorule_issue(43, "This evidence cannot have with_or_from values")


def _with_requirement(
    constraint: EcoUsageConstraint, annotation: Annotation, facts: AnnotationFacts
) -> WithPresence | None:
    """Return the `with_or_from` requirement, or `None` if it cannot be decided."""
    if constraint.with_presence is not WithPresence.CONDITIONAL:
        return constraint.with_presence
    condition = constraint.condition
    if condition is None:
        return None
    if isinstance(condition.test, EntityIsComplex):
        if facts.entity is None:
            return None
        holds = PROTEIN_CONTAINING_COMPLEX in facts.entity.type_checked_ancestors
    else:
        holds = any(
            re.fullmatch(condition.test.pattern, reference)
            for reference in annotation.references
        )
    return condition.when_true if holds else condition.when_false


def gorule_0000058(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report each extension that conforms to no `extensions-constraints.yaml` entry.

    An extension conforms when one entry for its relation lists its term's
    prefix among `namespaces`, has a `primary_root_terms` entry the annotated
    term falls under, and has no `cardinality` or one no smaller than the number
    of the annotation's extensions with the same relation and prefix. An
    extension whose relation has no entry does not conform. The annotated term
    must be current, because its checked ancestors decide the roots.
    """
    data = facts.rule_data
    extensions = annotation.annotation_extensions or []
    term = annotated_term(annotation, facts)
    if data is None or term is None or not extensions:
        return
    counts = Counter(
        (extension.extension_relation, split_identifier(extension.extension_term)[0])
        for extension in extensions
    )
    for index, extension in enumerate(extensions):
        namespace = split_identifier(extension.extension_term)[0]
        count = counts[(extension.extension_relation, namespace)]
        conforms = any(
            constraint.relation_id == extension.extension_relation
            and namespace in constraint.namespaces
            and term.checked_ancestors.intersection(constraint.primary_root_terms)
            and (constraint.cardinality is None or count <= constraint.cardinality)
            for constraint in data.extension_constraints
        )
        if not conforms:
            yield gorule_issue(
                58,
                "Extension does not conform to extensions-constraints.yaml",
                (ANNOTATION_ROOT, "annotation_extensions", index),
            )
