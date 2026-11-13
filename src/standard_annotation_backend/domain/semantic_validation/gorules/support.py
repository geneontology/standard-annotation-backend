"""Check the GO Rules about `with_or_from` values and references."""

from collections.abc import Iterator

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.details import (
    PrimaryIdDetails,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    AnnotationFacts,
)
from standard_annotation_backend.domain.semantic_validation.gorules.common import (
    evidence_code,
    gorule_issue,
)
from standard_annotation_backend.domain.semantic_validation.identifiers import (
    split_identifier,
)
from standard_annotation_backend.domain.validation import (
    ANNOTATION_ROOT,
    ValidationIssue,
)

_SIMILARITY_EVIDENCE = frozenset({"ISS", "ISA", "ISO"})
_REFERENCE_PREFIXES = frozenset({"PMID", "DOI", "GO_REF"})
PHYLOGENETIC_REFERENCE = "GO_REF:0000033"
"""The only reference IBA annotations may cite (GORULE:0000037)."""


def _reference_location(index: int) -> tuple[str | int, ...]:
    return (ANNOTATION_ROOT, "references", index)


def gorule_0000016(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report IC evidence without a GO term in `with_or_from`.

    The annotated term itself does not count.
    """
    if evidence_code(annotation) != "IC":
        return
    inferred_from = [
        value
        for value in annotation.with_or_from or []
        if split_identifier(value)[0] == "GO" and value != annotation.ontology_class_id
    ]
    if not inferred_from:
        yield gorule_issue(
            16,
            "IC evidence requires a GO term in with_or_from other than the annotated term",
        )


def gorule_0000017(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report IDA evidence with `with_or_from` values."""
    if evidence_code(annotation) == "IDA" and annotation.with_or_from:
        yield gorule_issue(17, "IDA evidence cannot have with_or_from values")


def gorule_0000018(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report IPI evidence without `with_or_from` values."""
    if evidence_code(annotation) == "IPI" and not annotation.with_or_from:
        yield gorule_issue(18, "IPI evidence requires with_or_from values")


def gorule_0000022(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report each reference listed in `retracted-publications.txt`."""
    for index, reference in enumerate(annotation.references):
        if reference in facts.retracted_pmids:
            yield gorule_issue(
                22, "Reference is a retracted publication", _reference_location(index)
            )


def gorule_0000030(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report a `GO_REF:` reference that `gorefs.yaml` does not list as current.

    A reference is reported when it is absent, marked obsolete, or an
    alternative ID; an alternative ID's issue names its primary reference.
    """
    if facts.rule_data is None:
        return
    entries = {entry.go_ref_id: entry for entry in facts.rule_data.go_refs}
    primary_by_alt_id = {
        alt_id: entry.go_ref_id
        for entry in facts.rule_data.go_refs
        for alt_id in entry.alt_ids
    }
    for index, reference in enumerate(annotation.references):
        if split_identifier(reference)[0] != "GO_REF":
            continue
        location = _reference_location(index)
        if reference in primary_by_alt_id:
            yield gorule_issue(
                30,
                "GO_REF reference is an alternative ID",
                location,
                PrimaryIdDetails(primary_id=primary_by_alt_id[reference]),
            )
        elif reference not in entries:
            yield gorule_issue(
                30, "GO_REF reference is not listed in gorefs.yaml", location
            )
        elif entries[reference].obsolete:
            yield gorule_issue(30, "GO_REF reference is obsolete", location)


def gorule_0000033(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report each reference whose prefix is not `PMID`, `DOI`, or `GO_REF`."""
    for index, reference in enumerate(annotation.references):
        if split_identifier(reference)[0] not in _REFERENCE_PREFIXES:
            yield gorule_issue(
                33,
                "References must be PMID, DOI, or GO_REF identifiers",
                _reference_location(index),
            )


def gorule_0000037(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report IBA evidence with the wrong `assigned_by` or references.

    IBA annotations must have `assigned_by` set to `GO_Central` and cite
    `GO_REF:0000033` as their only reference.
    """
    if evidence_code(annotation) != "IBA":
        return
    if annotation.assigned_by != "GO_Central" or set(annotation.references) != {
        PHYLOGENETIC_REFERENCE
    }:
        yield gorule_issue(
            37,
            "IBA evidence requires assigned_by GO_Central and the single reference "
            "GO_REF:0000033",
        )


def gorule_0000050(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report ISS, ISA, or ISO evidence whose `with_or_from` contains `db_object_id`."""
    if evidence_code(
        annotation
    ) in _SIMILARITY_EVIDENCE and annotation.db_object_id in (
        annotation.with_or_from or []
    ):
        yield gorule_issue(
            50, "Sequence similarity evidence cannot list db_object_id in with_or_from"
        )


def gorule_0000055(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report each reference whose prefix an earlier, different reference used.

    The same reference listed twice is not reported.
    """
    seen_references: set[str] = set()
    seen_prefixes: set[str] = set()
    for index, reference in enumerate(annotation.references):
        if reference in seen_references:
            continue
        seen_references.add(reference)
        prefix = split_identifier(reference)[0]
        if prefix in seen_prefixes:
            yield gorule_issue(
                55,
                "Each reference must use a different prefix",
                _reference_location(index),
            )
        seen_prefixes.add(prefix)


def gorule_0000063(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report ISS, ISA, or ISO evidence without `with_or_from` values."""
    if (
        evidence_code(annotation) in _SIMILARITY_EVIDENCE
        and not annotation.with_or_from
    ):
        yield gorule_issue(
            63, "Sequence similarity evidence requires with_or_from values"
        )
