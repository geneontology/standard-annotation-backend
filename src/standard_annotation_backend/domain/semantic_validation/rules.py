"""Register every semantic rule and run them against an annotation's facts.

A rule pairs one issue type with the check that reports it. Each check takes an
annotation and its facts and returns the issues it finds, without reading anything
else. `RULES` lists every rule once, in the order its issues are reported: the
entity check, the term checks (unknown, obsolete, then alternative identifiers), the
duplicate check, then the GO Rules in rule-number order. The same annotation and
facts therefore always give the same issues in the same order.

The QC report issue catalog and the API's list of issue types are built from
`RULES`, so a rule's issue type, title, and details shape are declared only here.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.checks import (
    check_alternate_term_ids,
    check_duplicates,
    check_entity,
    check_obsolete_terms,
    check_unknown_terms,
)
from standard_annotation_backend.domain.semantic_validation.details import (
    AllowedRelationsDetails,
    DuplicatePeersDetails,
    ObsoleteTermDetails,
    PrimaryIdDetails,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    AnnotationFacts,
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
from standard_annotation_backend.domain.validation import ValidationIssue

type SemanticCheck = Callable[[Annotation, AnnotationFacts], Iterable[ValidationIssue]]
"""A function that returns the issues it finds in an annotation, given its facts."""

_GO_RULE_URL = (
    "https://github.com/geneontology/go-site/blob/master/metadata/rules/"
    "gorule-{number:07d}.md"
)


@dataclass(frozen=True, slots=True)
class Rule:
    """Declare one issue type and the check that reports it.

    Attributes:
        issue_type: The stable name of the issue type, such as `obsolete_term`.
        title: A stable, human-readable title. A GO Rule's title follows its
            go-site page.
        check: Returns the rule's issues. Every issue has `issue_type` as its type.
        details: The `TypedDict` from `semantic_validation.details` that the
            issues' `details` follow, or `None` when they never have details. A
            rule may send details for only some of its issues.
        blocks_writes: Whether an annotation with this issue cannot be written.
            A rule that does not block writes runs only in QC reports.
        link: The go-site page of a GO Rule; `None` for other rules.
    """

    issue_type: str
    title: str
    check: SemanticCheck
    details: type | None = None
    blocks_writes: bool = True
    link: str | None = None


def _go_rule(
    number: int, title: str, check: SemanticCheck, details: type | None = None
) -> Rule:
    """Return the rule for a GO Rule, named and linked by its number."""
    return Rule(
        issue_type=f"gorule_{number:07d}",
        title=title,
        check=check,
        details=details,
        link=_GO_RULE_URL.format(number=number),
    )


RULES: tuple[Rule, ...] = (
    Rule("unknown_entity", "Subject is not an active entity", check_entity),
    Rule(
        "unknown_term", "Identifier is not a term of its ontology", check_unknown_terms
    ),
    Rule(
        "obsolete_term", "Term is obsolete", check_obsolete_terms, ObsoleteTermDetails
    ),
    Rule(
        "alternate_term_id",
        "Identifier is an alternative ID",
        check_alternate_term_ids,
        PrimaryIdDetails,
    ),
    Rule(
        "duplicate_annotation",
        "Annotation duplicates other annotations",
        check_duplicates,
        DuplicatePeersDetails,
    ),
    _go_rule(
        2,
        "No NOT annotations to GO:0005488 binding or GO:0005515 protein binding",
        gorule_0000002,
    ),
    _go_rule(
        5,
        "IEA, ISS, ISO, ISM, ISA, IBA, RCA not allowed for direct binding or "
        "protein binding",
        gorule_0000005,
    ),
    _go_rule(6, "IEP and HEP only for Biological Process terms", gorule_0000006),
    _go_rule(
        7,
        "IPI should not be used with GO:0003824 catalytic activity or descendants",
        gorule_0000007,
    ),
    _go_rule(8, "No annotations to uninformative high-level terms", gorule_0000008),
    _go_rule(11, "ND only on root terms, and root terms only with ND", gorule_0000011),
    _go_rule(15, "Dual species taxon check", gorule_0000015),
    _go_rule(16, "IC annotations require a With/From GO ID", gorule_0000016),
    _go_rule(17, "IDA annotations must not have a With/From entry", gorule_0000017),
    _go_rule(18, "IPI annotations require a With/From entry", gorule_0000018),
    _go_rule(22, "Filter annotations to retracted publications", gorule_0000022),
    _go_rule(27, "Each identifier in GAF is valid", gorule_0000027),
    _go_rule(30, "Only valid GO_REFs are allowed", gorule_0000030, PrimaryIdDetails),
    _go_rule(33, "Only public reference IDs (PMID, DOI, GO_REF)", gorule_0000033),
    _go_rule(37, "IBA only assigned_by GO_Central with GO_REF:0000033", gorule_0000037),
    _go_rule(
        39,
        "Protein complexes cannot be annotated to GO:0032991 or descendants",
        gorule_0000039,
    ),
    _go_rule(42, "IKR evidence requires a NOT qualifier", gorule_0000042),
    _go_rule(
        43,
        "Check for valid combination of evidence code, with/from rules and GO_REF",
        gorule_0000043,
    ),
    _go_rule(
        46,
        "With/from must equal the gene product for self-binding terms",
        gorule_0000046,
    ),
    _go_rule(50, "ISS, ISA, ISO should not be self-referential", gorule_0000050),
    _go_rule(53, "NOT annotations should not have extensions", gorule_0000053),
    _go_rule(55, "References should have only one ID per ID space", gorule_0000055),
    _go_rule(58, "Extensions conform to extensions-constraints.yaml", gorule_0000058),
    _go_rule(
        61,
        "Allowed gene product to term relations",
        gorule_0000061,
        AllowedRelationsDetails,
    ),
    _go_rule(
        63, "ISS/ISA/ISO evidence requires a with/from gene product", gorule_0000063
    ),
    _go_rule(
        65, "Annotations to obsoletion candidates produce a warning", gorule_0000065
    ),
)
"""Every semantic rule, in the order its issues are reported."""


def validate_semantics(
    annotation: Annotation,
    facts: AnnotationFacts,
    *,
    include_report_only: bool = False,
) -> tuple[ValidationIssue, ...]:
    """Run the rules in `RULES` order and return all of their issues.

    Args:
        annotation: A structurally valid annotation.
        facts: The facts gathered about it.
        include_report_only: Also run rules that do not block writes. QC reports
            set this; writes and change-set previews do not.

    Returns:
        Every issue, in `RULES` order.

    Raises:
        ValueError: If a check reports an issue type other than its rule's. This
            is a programming error, not an invalid annotation.
    """
    issues: list[ValidationIssue] = []
    for rule in RULES:
        if not (rule.blocks_writes or include_report_only):
            continue
        for issue in rule.check(annotation, facts):
            if issue["type"] != rule.issue_type:
                raise ValueError(
                    f"Rule {rule.issue_type} reported an issue of type {issue['type']}"
                )
            issues.append(issue)
    return tuple(issues)
