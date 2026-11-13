"""Define ontology documents, snapshots, closures, and refresh results.

A snapshot is the set of terms and relationships SABE read from one fetched
ontology file. Each ontology has at most one active snapshot, which validation and
search use. A closure lists, for each term, every term it reaches by following one
kind of relationship any number of steps, such as all of a term's `is_a`
ancestors.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field

from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    TerminalRefreshError,
)
from standard_annotation_backend.domain.stored_json import StoredJson

GO_ONTOLOGY_KEY = "go"
"""Key of the GO ontology in `config/sources.yaml`.

Every deployment configures GO. Code that works with GO specifically, such as
searching annotations by a GO term and its descendants, finds GO's data by this key.
"""

SUBCLASS_PREDICATE = "rdfs:subClassOf"
"""Predicate SABE uses for OBO `is_a` relationships when computing closure."""

REQUIRED_GO_SUBSETS = ("gocheck_do_not_annotate", "gocheck_obsoletion_candidate")
"""GO subsets that GO Rules check annotations against.

GORULE:0000008 uses `gocheck_do_not_annotate` and GORULE:0000065 uses
`gocheck_obsoletion_candidate`. If a GO file did not declare them, those rules
would silently find nothing, so GO's entry in `config/sources.yaml` must list both
in its `required_subsets`.
"""


class OntologyCandidateConflictError(TerminalRefreshError):
    """Report that a staged job conflicts with its source or activation order."""

    failure_code = RefreshFailureCode.CANDIDATE_CONFLICT


@dataclass(frozen=True, slots=True)
class OntologyDefinition:
    """Describe how SABE reads one configured ontology.

    Attributes:
        key: The ontology's key in `config/sources.yaml`, such as `go`.
        identifier_prefixes: Identifier prefixes the ontology owns, such as `GO`.
            Terms with other prefixes in the same file are ignored.
        closure_predicates: Relationships whose closure SABE computes, such as
            `rdfs:subClassOf`. Empty when the ontology is loaded as a term list.
        document_ontology_id: The `ontology:` header value the file must declare.
        required_subsets: Subsets the file must declare. A refresh fails when the
            file lacks one.
    """

    key: str
    identifier_prefixes: tuple[str, ...]
    closure_predicates: tuple[str, ...]
    document_ontology_id: str
    required_subsets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OntologyDocument:
    """Contain retrieved ontology bytes and the source details that identify them."""

    ontology_key: str
    content: bytes
    source_type: str
    source_locator: str
    source_revision: str | None
    source_checksum: str
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class OntologyTerm:
    """Describe one term: its status, replacement suggestions, and subsets.

    Attributes:
        term_id: The term's identifier, such as `GO:0005515`.
        obsolete: Whether the ontology marks the term obsolete.
        replaced_by: The terms the ontology says to use instead, as published.
            They may be obsolete or belong to another ontology.
        consider: Terms the ontology suggests considering instead.
        subsets: Names of the ontology subsets the term belongs to, such as
            `gocheck_do_not_annotate`.
    """

    term_id: str
    obsolete: bool
    replaced_by: tuple[str, ...]
    consider: tuple[str, ...]
    subsets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, order=True)
class OntologyEdge:
    """Represent one direct relationship used to calculate ontology closure."""

    subject_term_id: str
    predicate_id: str
    object_term_id: str


@dataclass(frozen=True, slots=True)
class OntologySnapshot:
    """Contain everything SABE reads from one ontology document.

    Attributes:
        document: The fetched document the snapshot was parsed from.
        document_version: The document's `data-version`, if it declares one.
        closure_predicates: Relationships whose closure SABE computes.
        identifier_prefixes: The ontology's identifier prefixes when the file was
            read. Only terms with these prefixes are included.
        terms: The ontology's terms by identifier.
        edges: Direct relationships used to compute closure.
        alt_ids: Each alternative identifier mapped to its primary term. When an
            ontology merges terms, it keeps the identifiers of the merged terms as
            alternative identifiers of the remaining one. Only alternative
            identifiers that name exactly one term are included.
        declared_subsets: Subset names the document's header declares.
    """

    document: OntologyDocument
    document_version: str | None
    closure_predicates: tuple[str, ...]
    terms: Mapping[str, OntologyTerm]
    edges: tuple[OntologyEdge, ...]
    alt_ids: Mapping[str, str] = field(default_factory=dict)
    declared_subsets: frozenset[str] = frozenset()
    identifier_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, order=True)
class OntologyClosureRow:
    """Record the shortest path length between two terms for one predicate."""

    subject_term_id: str
    predicate_id: str
    object_term_id: str
    depth: int


class OntologyParseError(ValueError):
    """Report that source bytes cannot form a complete ontology snapshot."""


@dataclass(frozen=True, slots=True, order=True)
class OntologyRefreshWarning:
    """Describe a nonfatal condition found in loaded ontology data."""

    code: str
    term_id: str
    referenced_term_id: str


def ontology_refresh_warnings(
    terms: Mapping[str, OntologyTerm],
    identifier_prefixes: tuple[str, ...],
) -> tuple[OntologyRefreshWarning, ...]:
    """Return warnings for `consider` targets the snapshot should define but does not.

    Only targets with one of the ontology's own prefixes are checked. A suggestion
    that names a term of another ontology cannot be checked against this snapshot.
    """
    return tuple(
        OntologyRefreshWarning(
            code="undefined_consider_target",
            term_id=term.term_id,
            referenced_term_id=target,
        )
        for term in sorted(terms.values(), key=lambda value: value.term_id)
        for target in sorted(set(term.consider))
        if target not in terms and target.partition(":")[0] in identifier_prefixes
    )


def missing_required_subsets(
    snapshot: OntologySnapshot, definition: OntologyDefinition
) -> tuple[str, ...]:
    """Return the subsets an ontology's entry requires that its file does not declare.

    Args:
        snapshot: The parsed file.
        definition: The ontology's entry in `config/sources.yaml`.

    Returns:
        The missing subset names, in the order the entry lists them. Empty when the
        file declares every required subset.
    """
    return tuple(
        subset
        for subset in definition.required_subsets
        if subset not in snapshot.declared_subsets
    )


@dataclass(frozen=True, slots=True)
class OntologyVersion:
    """Describe one stored ontology snapshot returned by the service layer."""

    version_id: UUID
    ontology_key: str
    source_type: str
    source_locator: str
    source_revision: str | None
    source_checksum: str
    document_version: str | None
    loaded_predicates: tuple[str, ...]
    fetched_at: datetime
    term_count: int
    closure_count: int
    active: bool


@dataclass(frozen=True, slots=True)
class OntologyRefreshResult:
    """Summarize an ontology refresh: the snapshot it activated, if any, and warnings.

    `ontology_version` is `None` when the fetched document matched the active
    snapshot, so nothing was activated.
    """

    ontology_version: OntologyVersion | None
    document: OntologyDocument | None = None
    ontology_warnings: tuple[OntologyRefreshWarning, ...] = ()

    @classmethod
    def for_active_document(cls, document: OntologyDocument) -> OntologyRefreshResult:
        """Return a refresh result for a document matching the active snapshot."""
        return cls(None, document)

    def to_stored(self) -> StoredOntologyRefresh:
        """Return the outcome in the form stored on snapshots and jobs.

        Snapshot details are used when a snapshot was activated. Otherwise only the
        fetched document's source details are known.
        """
        version = self.ontology_version
        document = self.document
        source = version if version is not None else document
        return StoredOntologyRefresh(
            ontology=source.ontology_key if source is not None else None,
            ontology_version_id=version.version_id if version is not None else None,
            source_type=source.source_type if source is not None else None,
            source_locator=source.source_locator if source is not None else None,
            source_revision=source.source_revision if source is not None else None,
            source_checksum=source.source_checksum if source is not None else None,
            document_version=version.document_version if version is not None else None,
            loaded_predicates=version.loaded_predicates if version is not None else (),
            term_count=version.term_count if version is not None else 0,
            closure_count=version.closure_count if version is not None else 0,
            ontology_warnings=self.ontology_warnings,
        )

    def to_job_result(self) -> dict[str, object]:
        """Return the complete successful outcome in job-storage format."""
        return STORED_ONTOLOGY_REFRESH_RESULT.dump(self.to_stored())


@dataclass(frozen=True, slots=True)
class StoredOntologyRefresh:
    """Describe an ontology refresh result as stored on its snapshot and job.

    The snapshot's details are written at the top level. When the fetched document
    already matched the active snapshot, only the document's source details are
    known, so the snapshot fields are `None` or empty and both counts are zero.

    Attributes:
        ontology: The refreshed ontology.
        ontology_version_id: The activated snapshot, or `None` if nothing was
            activated.
        source_type: How the source document was retrieved.
        source_locator: Where the source document was retrieved from.
        source_revision: The source's revision identifier, if it has one.
        source_checksum: The checksum of the source document.
        document_version: The version declared by the ontology document, if any.
        loaded_predicates: The relationship predicates loaded into the closure.
        term_count: The number of terms in the snapshot.
        closure_count: The number of closure rows in the snapshot.
        ontology_warnings: Nonfatal conditions found in the loaded ontology.
    """

    ontology: str | None
    ontology_version_id: UUID | None
    source_type: str | None
    source_locator: str | None
    source_revision: str | None
    source_checksum: str | None
    document_version: str | None
    loaded_predicates: tuple[str, ...]
    term_count: Annotated[int, Field(ge=0)]
    closure_count: Annotated[int, Field(ge=0)]
    ontology_warnings: tuple[OntologyRefreshWarning, ...]


STORED_ONTOLOGY_REFRESH_RESULT = StoredJson(
    StoredOntologyRefresh, label="ontology refresh result"
)


def compute_closure(snapshot: OntologySnapshot) -> Iterator[OntologyClosureRow]:
    """Yield shortest path lengths between terms for each configured predicate.

    Args:
        snapshot: Parsed terms, approved predicates, and direct edges.

    Yields:
        Rows ordered by predicate, subject term, object term, and depth. Paths
        using different predicates are calculated separately.
    """
    adjacency: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for edge in snapshot.edges:
        adjacency[edge.predicate_id][edge.subject_term_id].add(edge.object_term_id)

    term_ids = tuple(sorted(snapshot.terms))
    for predicate_id in sorted(snapshot.closure_predicates):
        for subject_term_id in term_ids:
            depths = {subject_term_id: 0}
            pending = deque([subject_term_id])
            while pending:
                current = pending.popleft()
                next_depth = depths[current] + 1
                for object_term_id in sorted(adjacency[predicate_id][current]):
                    previous_depth = depths.get(object_term_id)
                    if previous_depth is not None and previous_depth <= next_depth:
                        continue
                    depths[object_term_id] = next_depth
                    pending.append(object_term_id)
            yield from (
                OntologyClosureRow(
                    subject_term_id=subject_term_id,
                    predicate_id=predicate_id,
                    object_term_id=object_term_id,
                    depth=depth,
                )
                for object_term_id, depth in sorted(depths.items())
            )
