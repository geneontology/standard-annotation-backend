"""Build annotations and facts for the GO Rule unit tests.

`annotation()` with `facts()` passes every GO Rule. Each test changes only what
its rule examines, so a failing assertion points at that rule.
"""

from collections.abc import Iterable, Mapping

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


def found(issues: Iterable[ValidationIssue]) -> list[tuple[str, tuple[str | int, ...]]]:
    """Return each issue's type and location, in order."""
    return [(issue["type"], issue["location"]) for issue in issues]
