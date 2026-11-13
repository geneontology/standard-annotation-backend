"""Check the GO Rules about the annotated term, evidence, and relation.

A term is under another GO term when it is that term or one of its
`rdfs:subClassOf` descendants.
"""

from collections.abc import Iterator

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    BP_ROOT,
    CATALYTIC_ACTIVITY,
    CC_ROOT,
    CELLULAR_ANATOMICAL_STRUCTURE,
    GO_ROOTS,
    INTERSPECIES_INTERACTION_TERMS,
    MF_ROOT,
    PROTEIN_CONTAINING_COMPLEX,
    SELF_BINDING_TERMS,
    VIRION_COMPONENT,
)
from standard_annotation_backend.domain.semantic_validation.details import (
    AllowedRelationsDetails,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    AnnotationFacts,
)
from standard_annotation_backend.domain.semantic_validation.gorules.common import (
    annotated_term,
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

BINDING_TERMS = frozenset({"GO:0005488", "GO:0005515"})
"""Binding and protein binding, which GORULE:0000002 and GORULE:0000005 name.

These rules match the two terms exactly, not their descendants.
"""

_INFERRED_BINDING_EVIDENCE = frozenset({"IEA", "ISS", "ISO", "ISM", "ISA", "IBA"})
_TERM_LOCATION = (ANNOTATION_ROOT, "ontology_class_id")

_MF_RELATIONS = frozenset({"RO:0002327", "RO:0002326"})
_BP_RELATIONS = frozenset(
    {
        "RO:0002331",
        "RO:0002264",
        "RO:0004032",
        "RO:0004033",
        "RO:0002263",
        "RO:0004034",
        "RO:0004035",
    }
)
_COMPLEX_RELATIONS = frozenset({"BFO:0000050"})
_ANATOMY_RELATIONS = frozenset({"RO:0001025", "RO:0002432", "RO:0002325"})
_ROOT_RELATIONS = {
    MF_ROOT: frozenset({"RO:0002327"}),
    BP_ROOT: frozenset({"RO:0002331"}),
    CC_ROOT: frozenset({"RO:0002432"}),
}


def gorule_0000002(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report a negated annotation directly to `GO:0005488` or `GO:0005515`."""
    if (
        annotated_term(annotation, facts) is not None
        and annotation.negation
        and annotation.ontology_class_id in BINDING_TERMS
    ):
        yield gorule_issue(
            2, "Annotations to GO:0005488 or GO:0005515 cannot be negated"
        )


def gorule_0000005(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report IEA, ISS, ISO, ISM, ISA, or IBA evidence on a binding term.

    The terms are `GO:0005488` and `GO:0005515`, matched directly. RCA is not
    included: the rule's title lists it, but its body does not.
    """
    if (
        annotated_term(annotation, facts) is not None
        and annotation.ontology_class_id in BINDING_TERMS
        and evidence_code(annotation) in _INFERRED_BINDING_EVIDENCE
    ):
        yield gorule_issue(
            5,
            "Annotations to GO:0005488 or GO:0005515 cannot use IEA, ISS, ISO, "
            "ISM, ISA, or IBA evidence",
        )


def gorule_0000006(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report IEP or HEP evidence on a term that is not a biological process term."""
    term = annotated_term(annotation, facts)
    if (
        term is not None
        and evidence_code(annotation) in {"IEP", "HEP"}
        and BP_ROOT not in term.checked_ancestors
    ):
        yield gorule_issue(
            6, "IEP and HEP evidence support only biological process terms"
        )


def gorule_0000007(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report IPI evidence on a term under `GO:0003824` (catalytic activity)."""
    term = annotated_term(annotation, facts)
    if (
        term is not None
        and evidence_code(annotation) == "IPI"
        and CATALYTIC_ACTIVITY in term.checked_ancestors
    ):
        yield gorule_issue(7, "IPI evidence cannot support catalytic activity terms")


def gorule_0000008(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report a term in the `gocheck_do_not_annotate` subset."""
    term = annotated_term(annotation, facts)
    if term is not None and "gocheck_do_not_annotate" in term.subsets:
        yield gorule_issue(
            8, "Term is in the gocheck_do_not_annotate subset", _TERM_LOCATION
        )


def gorule_0000011(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report ND evidence on a non-root term, or other evidence on a root term."""
    if annotated_term(annotation, facts) is None:
        return
    is_root = annotation.ontology_class_id in GO_ROOTS
    if is_root != (evidence_code(annotation) == "ND"):
        yield gorule_issue(
            11, "ND evidence must be used with a GO root term, and only with one"
        )


def gorule_0000015(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report disallowed interacting taxa, or ones equal to the entity's taxon.

    Interacting taxa are allowed only for terms under `GO:0044419`,
    `GO:0043903`, or `GO:0018995`. A taxon equal to the entity's own
    `db_object_taxon_id` is reported at its position in `interacting_taxon_id`.
    That part is not checked when the entity is unknown.
    """
    taxa = annotation.interacting_taxon_id or []
    if not taxa:
        return
    term = annotated_term(annotation, facts)
    if term is not None and not term.checked_ancestors & INTERSPECIES_INTERACTION_TERMS:
        yield gorule_issue(
            15,
            "Interacting taxa require a term under GO:0044419, GO:0043903, or "
            "GO:0018995",
        )
    if facts.entity is not None:
        for index, taxon in enumerate(taxa):
            if taxon == facts.entity.db_object_taxon_id:
                yield gorule_issue(
                    15,
                    "An interacting taxon cannot be the annotated entity's own taxon",
                    (ANNOTATION_ROOT, "interacting_taxon_id", index),
                )


def gorule_0000039(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report a ComplexPortal entity annotated to a term under `GO:0032991`.

    `GO:0032991` is protein-containing complex.
    """
    term = annotated_term(annotation, facts)
    if (
        term is not None
        and split_identifier(annotation.db_object_id)[0] == "ComplexPortal"
        and PROTEIN_CONTAINING_COMPLEX in term.checked_ancestors
    ):
        yield gorule_issue(
            39, "ComplexPortal entities cannot be annotated to complex terms"
        )


def gorule_0000042(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report IKR evidence on an annotation that is not negated."""
    if evidence_code(annotation) == "IKR" and annotation.negation is not True:
        yield gorule_issue(42, "IKR evidence requires a negated annotation")


def gorule_0000046(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report a self-binding annotation whose `with_or_from` omits its subject.

    The rule applies only when `with_or_from` has values and the term is under
    one of `SELF_BINDING_TERMS`. Then `with_or_from` must contain
    `db_object_id`.
    """
    term = annotated_term(annotation, facts)
    with_or_from = annotation.with_or_from or []
    if (
        term is not None
        and term.checked_ancestors & SELF_BINDING_TERMS
        and with_or_from
        and annotation.db_object_id not in with_or_from
    ):
        yield gorule_issue(
            46,
            "Self-binding annotations with with_or_from values must include db_object_id",
        )


def gorule_0000053(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report a negated annotation that has any extension."""
    if annotation.negation and annotation.annotation_extensions:
        yield gorule_issue(53, "Negated annotations cannot have extensions")


def gorule_0000061(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report a relation that GORULE:0000061 does not allow for the annotated term.

    `allowed_relations` decides which relations are allowed, and the issue lists
    them. Negated annotations are checked the same way.
    """
    term = annotated_term(annotation, facts)
    if term is None:
        return
    allowed = allowed_relations(annotation.ontology_class_id, term.checked_ancestors)
    if annotation.relation not in allowed:
        yield gorule_issue(
            61,
            "Relation is not allowed for the term's branch",
            (ANNOTATION_ROOT, "relation"),
            AllowedRelationsDetails(allowed_relations=sorted(allowed)),
        )


def allowed_relations(
    term_id: str, checked_ancestors: frozenset[str]
) -> frozenset[str]:
    """Return the relations GORULE:0000061 allows for a term.

    Each of the three GO root terms has its own relations. Any other molecular
    function term gets the molecular function relations, and any other biological
    process term the biological process relations. Any other term gets the
    protein-containing complex relations if it is under `GO:0032991`, plus the
    anatomical structure relations if it is under `GO:0110165` (cellular
    anatomical structure) or `GO:0044423` (virion component). A term under none
    of these gets no relations.

    Args:
        term_id: The annotated term.
        checked_ancestors: The checked ancestors the term falls under.
    """
    if term_id in _ROOT_RELATIONS:
        return _ROOT_RELATIONS[term_id]
    if MF_ROOT in checked_ancestors:
        return _MF_RELATIONS
    if BP_ROOT in checked_ancestors:
        return _BP_RELATIONS
    allowed: frozenset[str] = frozenset()
    if PROTEIN_CONTAINING_COMPLEX in checked_ancestors:
        allowed |= _COMPLEX_RELATIONS
    if checked_ancestors & {CELLULAR_ANATOMICAL_STRUCTURE, VIRION_COMPONENT}:
        allowed |= _ANATOMY_RELATIONS
    return allowed


def gorule_0000065(
    annotation: Annotation, facts: AnnotationFacts
) -> Iterator[ValidationIssue]:
    """Report a term in the `gocheck_obsoletion_candidate` subset."""
    term = annotated_term(annotation, facts)
    if term is not None and "gocheck_obsoletion_candidate" in term.subsets:
        yield gorule_issue(
            65, "Term is in the gocheck_obsoletion_candidate subset", _TERM_LOCATION
        )
