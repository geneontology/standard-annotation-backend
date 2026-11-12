"""Decide how obsolete terms and alternative IDs are replaced in annotations.

The `term_replacement` job uses these rules after an ontology activation or an
annotation publication. An identifier is replaceable when it is obsolete with
exactly one `replaced_by`, or when it is an alternative ID of a primary term.
Chains of such identifiers are followed within the identifier's own ontology to
the current term at their end. A chain that ends at a term that is not current,
leaves the ontology, or loops makes no replacement.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from pydantic import Field

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.semantic_validation.facts import (
    TermFact,
    TermStatus,
)
from standard_annotation_backend.domain.semantic_validation.identifiers import (
    split_identifier,
    term_identifiers,
)
from standard_annotation_backend.domain.stored_json import StoredJson

type Count = Annotated[int, Field(ge=0)]


def replacement_step(fact: TermFact) -> str | None:
    """Return the identifier one step along a replacement chain, if there is one.

    That is an alternative ID's primary term, or an obsolete term's only
    `replaced_by`.
    """
    if fact.status is TermStatus.ALTERNATE:
        return fact.primary_id
    if fact.status is TermStatus.OBSOLETE and len(fact.replaced_by) == 1:
        return fact.replaced_by[0]
    return None


def replacement_for(
    identifier: str,
    terms: Mapping[str, TermFact],
    ontology_of: Mapping[str, str],
) -> str | None:
    """Return the current term that replaces `identifier`, or `None`.

    Args:
        identifier: An identifier an annotation uses.
        terms: Facts about the identifier and every term its chain reaches, each
            from the active snapshot of the ontology that owns its prefix.
        ontology_of: The key of the configured ontology that owns each prefix.

    Returns:
        The current term at the end of the identifier's chain, or `None` when
        the identifier is current, is not replaceable, or its chain dead-ends,
        loops, or leaves the identifier's ontology.
    """
    ontology = ontology_of.get(split_identifier(identifier)[0])
    if ontology is None:
        return None
    seen = {identifier}
    current = identifier
    while True:
        fact = terms.get(current)
        if fact is None:
            return None
        if fact.status is TermStatus.CURRENT:
            return None if current == identifier else current
        following = replacement_step(fact)
        if (
            following is None
            or following in seen
            or ontology_of.get(split_identifier(following)[0]) != ontology
        ):
            return None
        seen.add(following)
        current = following


def replaced_annotation(
    annotation: Annotation,
    terms: Mapping[str, TermFact],
    ontology_of: Mapping[str, str],
) -> Annotation | None:
    """Return the annotation with every replaceable identifier replaced.

    Every field the term checks cover is examined: `ontology_class_id`, each
    extension term, each `with_or_from` value, and each interacting taxon.
    When a replacement repeats a value already in its list, only the first copy
    is kept.

    Returns:
        The replaced annotation, or `None` when nothing would change.

    Raises:
        ValidationError: If a replacement makes the annotation structurally
            invalid, for example because a `replaced_by` value is malformed.
    """
    data = annotation.model_dump(mode="json")
    changed_lists: set[str] = set()
    changed = False
    for occurrence in term_identifiers(annotation):
        target = replacement_for(occurrence.identifier, terms, ontology_of)
        if target is None:
            continue
        path = occurrence.location[1:]
        _set_value(data, path, target)
        changed = True
        if len(path) > 1:
            changed_lists.add(str(path[0]))
    for name in changed_lists:
        data[name] = _first_copies(data[name])
    return Annotation.model_validate(data) if changed else None


def _first_copies(values: list[Any]) -> list[Any]:
    """Return the list without repeated values, keeping each one's first position.

    A replacement can turn one entry into a value the list already holds, such
    as an alternative ID whose primary term is also listed.
    """
    seen: set[str] = set()
    kept: list[Any] = []
    for value in values:
        key = json.dumps(value, sort_keys=True)
        if key not in seen:
            seen.add(key)
            kept.append(value)
    return kept


def _set_value(data: dict[str, Any], path: tuple[str | int, ...], value: str) -> None:
    """Replace the value at `path`, a location without its `annotation` root."""
    container: Any = data
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value


@dataclass(frozen=True, slots=True)
class TermReplacementResult:
    """Summarize one term replacement job.

    Attributes:
        group_key: The group the job covered, or `None` for all groups.
        annotation_scan_count: Active annotations read.
        annotation_replaced_count: Annotations given a new version.
        annotation_issue_skip_count: Annotations left unchanged because the
            replaced result had semantic issues or was not a valid annotation,
            or because the stored annotation could not be read.
        annotation_stale_skip_count: Annotations left unchanged because they
            changed after the job read them.
        snapshot_ids: Every snapshot ID the job read, for each ontology key and
            for `rules`.
    """

    group_key: str | None
    annotation_scan_count: Count
    annotation_replaced_count: Count
    annotation_issue_skip_count: Count
    annotation_stale_skip_count: Count
    snapshot_ids: dict[str, tuple[UUID, ...]]

    def to_progress(self) -> dict[str, int]:
        """Return the counts, as job progress shows them."""
        return {
            "annotation_scan_count": self.annotation_scan_count,
            "annotation_replaced_count": self.annotation_replaced_count,
            "annotation_issue_skip_count": self.annotation_issue_skip_count,
            "annotation_stale_skip_count": self.annotation_stale_skip_count,
        }

    def to_job_result(self) -> dict[str, object]:
        """Return the result in job-storage format."""
        return STORED_TERM_REPLACEMENT_RESULT.dump(self)


STORED_TERM_REPLACEMENT_RESULT = StoredJson(
    TermReplacementResult, label="term replacement result"
)
"""The stored form of a term replacement job's result."""
