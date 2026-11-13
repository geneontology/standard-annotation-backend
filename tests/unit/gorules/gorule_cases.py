"""Build annotations and facts for the GO Rule unit tests.

`annotation()` with `facts()` passes every GO Rule. Each test changes only what
its rule examines, so a failing assertion points at that rule.
"""

from collections.abc import Iterable, Mapping

from pydantic import TypeAdapter

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.schema_artifacts import load_evidence_codes
from standard_annotation_backend.domain.semantic_validation.checked_ancestors import (
    BP_ROOT,
)
from standard_annotation_backend.domain.semantic_validation.facts import (
    AnnotationFacts,
    EntityFact,
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    DatabaseEntry,
    RuleData,
)
from standard_annotation_backend.domain.semantic_validation.rules import (
    RULES,
    SemanticCheck,
    validate_semantics,
)
from standard_annotation_backend.domain.validation import ValidationIssue

TERM = "GO:0006915"
"""An ordinary biological process term, under the BP root only."""

ENTITY = EntityFact(db_object_taxon_id="NCBITaxon:7955", db_object_type="SO:0000704")
"""An active gene that is not a complex."""

ECO = {code: eco_id for eco_id, code in load_evidence_codes().items()}
"""The allowed ECO term for each GO evidence code, read from the schema."""

RULE_DATA = RuleData(
    databases=(DatabaseEntry("CL"), DatabaseEntry("GO"), DatabaseEntry("UniProtKB")),
    go_refs=(),
    group_shorthands=("GO_Central",),
    eco_usage_constraints=(),
    extension_constraints=(),
)
"""Rule data that the default annotation satisfies."""


def annotation(evidence: str = "IDA", **changes: object) -> Annotation:
    """Return a valid annotation with the given evidence code and fields replaced."""
    return Annotation.model_validate(
        {
            "db_object_id": "UniProtKB:P12345",
            "relation": "RO:0002331",
            "ontology_class_id": TERM,
            "references": ["PMID:1"],
            "evidence_type": ECO[evidence],
            "annotation_date": "2026-11-11",
            "assigned_by": "GO_Central",
            **changes,
        }
    )


def term(*checked_ancestors: str, subsets: Iterable[str] = ()) -> TermFact:
    """Return a current term that falls under `checked_ancestors` and is in `subsets`."""
    return TermFact(
        TermStatus.CURRENT,
        subsets=frozenset(subsets),
        checked_ancestors=frozenset(checked_ancestors),
    )


def facts(
    terms: Mapping[str, TermFact] | None = None,
    *,
    entity: EntityFact | None = ENTITY,
    rule_data: RuleData | None = RULE_DATA,
    retracted: Iterable[str] = (),
) -> AnnotationFacts:
    """Return facts in which `TERM` is a current biological process term."""
    return AnnotationFacts(
        terms={TERM: term(BP_ROOT)} if terms is None else terms,
        entity=entity,
        rule_data=rule_data,
        retracted_pmids=frozenset(retracted),
    )


def go_rule(issue_type: str) -> SemanticCheck:
    """Return a check that reports one rule's issues from full validation.

    The check runs `validate_semantics`, the validation that annotation writes and
    QC reports use, and keeps the issues of `issue_type`. A rule's tests therefore
    pass only if the rule is registered and the validator runs it. Details, when
    an issue has them, must have exactly the fields of the shape the rule
    declares; a rule that declares no shape never sends details.
    """
    (rule,) = [rule for rule in RULES if rule.issue_type == issue_type]

    def check(subject: Annotation, facts: AnnotationFacts) -> list[ValidationIssue]:
        issues = [
            issue
            for issue in validate_semantics(subject, facts, include_report_only=True)
            if issue["type"] == issue_type
        ]
        for issue in issues:
            if "details" in issue:
                assert rule.details is not None
                details = TypeAdapter(rule.details).validate_python(issue["details"])
                assert details == issue["details"]
        return issues

    return check


def found(issues: Iterable[ValidationIssue]) -> list[tuple[str, tuple[str | int, ...]]]:
    """Return each issue's type and location, in order."""
    return [(issue["type"], issue["location"]) for issue in issues]
