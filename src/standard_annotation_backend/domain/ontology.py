"""Define ontology documents, snapshots, closures, and replacement results."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.refresh import (
    RefreshFailureCode,
    TerminalRefreshError,
)
from standard_annotation_backend.domain.stored_json import StoredJson


class OntologyKey(StrEnum):
    """Identify ontologies configured for loading by SAB."""

    GO = "go"


class OntologyCandidateConflictError(TerminalRefreshError):
    """Report that a staged job conflicts with its source or activation order."""

    failure_code = RefreshFailureCode.CANDIDATE_CONFLICT


@dataclass(frozen=True, slots=True)
class OntologyDefinition:
    """Describe identifiers and relationship handling for an ontology."""

    key: OntologyKey
    identifier_prefixes: tuple[str, ...]
    closure_predicates: tuple[str, ...]
    document_ontology_id: str | None = None


@dataclass(frozen=True, slots=True)
class OntologyDocument:
    """Contain retrieved ontology bytes and the source details that identify them."""

    ontology_key: OntologyKey
    content: bytes
    source_type: str
    source_locator: str
    source_revision: str | None
    source_checksum: str
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class OntologyTerm:
    """Describe one term and the replacement metadata needed by SAB."""

    term_id: str
    obsolete: bool
    replaced_by: tuple[str, ...]
    consider: tuple[str, ...]


@dataclass(frozen=True, slots=True, order=True)
class OntologyEdge:
    """Represent one direct relationship used to calculate ontology closure."""

    subject_term_id: str
    predicate_id: str
    object_term_id: str


@dataclass(frozen=True, slots=True)
class OntologySnapshot:
    """Contain the parsed terms and approved direct edges from one document."""

    document: OntologyDocument
    document_version: str | None
    closure_predicates: tuple[str, ...]
    terms: Mapping[str, OntologyTerm]
    edges: tuple[OntologyEdge, ...]


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
) -> tuple[OntologyRefreshWarning, ...]:
    """Return warnings for advisory references to terms outside the snapshot."""
    return tuple(
        OntologyRefreshWarning(
            code="undefined_consider_target",
            term_id=term.term_id,
            referenced_term_id=target,
        )
        for term in sorted(terms.values(), key=lambda value: value.term_id)
        for target in sorted(set(term.consider))
        if target not in terms
    )


@dataclass(frozen=True, slots=True)
class OntologyFinding:
    """Describe an ontology condition that prevented an automatic update."""

    code: str
    ontology_key: OntologyKey
    term_id: str
    field_paths: tuple[str, ...]
    replacement_ids: tuple[str, ...] = ()
    annotation_id: UUID | None = None
    conflicting_annotation_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class OntologyReplacement:
    """Describe one term replacement and the annotation fields it changes."""

    term_id: str
    replacement_id: str
    field_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnnotationReplacementProposal:
    """Contain proposed annotation changes and findings that prevented changes."""

    annotation: Annotation
    changed_paths: tuple[str, ...]
    findings: tuple[OntologyFinding, ...]
    replacements: tuple[OntologyReplacement, ...]


@dataclass(frozen=True, slots=True)
class OntologyVersion:
    """Describe one stored ontology snapshot returned by the service layer."""

    version_id: UUID
    ontology_key: OntologyKey
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
    """Summarize ontology activation, warnings, and annotation updates."""

    ontology_version: OntologyVersion | None
    annotation_scan_count: int
    annotation_update_count: int
    annotation_skip_count: int
    findings: tuple[OntologyFinding, ...]
    document: OntologyDocument | None = None
    ontology_warnings: tuple[OntologyRefreshWarning, ...] = ()

    @classmethod
    def for_active_document(cls, document: OntologyDocument) -> OntologyRefreshResult:
        """Return a refresh result for a document matching the active snapshot."""
        return cls(None, 0, 0, 0, (), document)

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
            annotation_scan_count=self.annotation_scan_count,
            annotation_update_count=self.annotation_update_count,
            annotation_skip_count=self.annotation_skip_count,
            ontology_warnings=self.ontology_warnings,
            findings=tuple(
                StoredOntologyFinding(
                    code=finding.code,
                    ontology=finding.ontology_key,
                    term_id=finding.term_id,
                    annotation_id=finding.annotation_id,
                    field_paths=finding.field_paths,
                    replacement_ids=finding.replacement_ids,
                    conflicting_annotation_ids=finding.conflicting_annotation_ids,
                )
                for finding in self.findings
            ),
        )

    def to_job_result(self) -> dict[str, object]:
        """Return the complete successful outcome in job-storage format."""
        return STORED_ONTOLOGY_REFRESH_RESULT.dump(self.to_stored())


@dataclass(frozen=True, slots=True)
class StoredOntologyFinding:
    """Describe one finding as it is stored in an ontology refresh result.

    Attributes:
        code: The machine-readable finding code.
        ontology: The ontology the term belongs to.
        term_id: The term that caused the finding.
        annotation_id: The affected annotation, if the finding concerns one.
        field_paths: The annotation fields the finding concerns.
        replacement_ids: The replacement terms the ontology offers.
        conflicting_annotation_ids: Annotations that conflict with the update.
    """

    code: str
    ontology: OntologyKey
    term_id: str
    annotation_id: UUID | None
    field_paths: tuple[str, ...]
    replacement_ids: tuple[str, ...]
    conflicting_annotation_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class StoredOntologyRefresh:
    """Describe an ontology refresh result as stored on its snapshot and job.

    The snapshot's details are written at the top level. When the fetched document
    already matched the active snapshot, only the document's source details are
    known, so the snapshot fields are `None` or empty and every count is zero.

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
        annotation_scan_count: The number of annotations examined.
        annotation_update_count: The number of annotations updated.
        annotation_skip_count: The number of annotations left unchanged because of
            findings.
        ontology_warnings: Nonfatal conditions found in the loaded ontology.
        findings: Conditions that prevented automatic annotation updates.
    """

    ontology: OntologyKey | None
    ontology_version_id: UUID | None
    source_type: str | None
    source_locator: str | None
    source_revision: str | None
    source_checksum: str | None
    document_version: str | None
    loaded_predicates: tuple[str, ...]
    term_count: Annotated[int, Field(ge=0)]
    closure_count: Annotated[int, Field(ge=0)]
    annotation_scan_count: Annotated[int, Field(ge=0)]
    annotation_update_count: Annotated[int, Field(ge=0)]
    annotation_skip_count: Annotated[int, Field(ge=0)]
    ontology_warnings: tuple[OntologyRefreshWarning, ...]
    findings: tuple[StoredOntologyFinding, ...]


STORED_ONTOLOGY_REFRESH_RESULT = StoredJson(
    StoredOntologyRefresh, label="ontology refresh result"
)


def propose_term_replacements(
    annotation: Annotation,
    snapshot: OntologySnapshot,
    previous_terms: Mapping[str, OntologyTerm],
    definition: OntologyDefinition,
) -> AnnotationReplacementProposal:
    """Replace obsolete terms when the ontology provides one replacement.

    The function checks the primary ontology term and annotation extension terms.
    It records a finding instead of changing a term that is missing, has multiple
    replacements, only has `consider` suggestions, or has no replacement. It does
    not treat relation or evidence fields as ontology terms.
    """
    annotation_data = annotation.model_dump(mode="json")
    occurrences: list[tuple[str, str]] = [
        ("/ontology_class_id", annotation.ontology_class_id)
    ]
    extensions = annotation.annotation_extensions or []
    occurrences.extend(
        (f"/annotation_extensions/{index}/extension_term", extension.extension_term)
        for index, extension in enumerate(extensions)
    )

    changed_paths: list[str] = []
    replacement_paths: dict[tuple[str, str], list[str]] = {}
    grouped_findings: dict[tuple[str, str, tuple[str, ...]], list[str]] = {}
    for path, term_id in occurrences:
        term = snapshot.terms.get(term_id)
        if term is None:
            if not _belongs_to_ontology(term_id, snapshot, previous_terms, definition):
                continue
            finding_key = ("removed_term", term_id, ())
            grouped_findings.setdefault(finding_key, []).append(path)
            continue
        if not term.obsolete:
            continue
        if len(term.replaced_by) == 1:
            _set_term_path(annotation_data, path, term.replaced_by[0])
            changed_paths.append(path)
            replacement_paths.setdefault((term_id, term.replaced_by[0]), []).append(
                path
            )
            continue
        if len(term.replaced_by) > 1:
            finding_key = (
                "ambiguous_replacement",
                term_id,
                term.replaced_by,
            )
        elif term.consider:
            finding_key = ("consider_only", term_id, term.consider)
        else:
            finding_key = ("obsolete_without_replacement", term_id, ())
        grouped_findings.setdefault(finding_key, []).append(path)

    findings = tuple(
        OntologyFinding(
            code=code,
            ontology_key=definition.key,
            term_id=term_id,
            field_paths=tuple(paths),
            replacement_ids=replacement_ids,
        )
        for (code, term_id, replacement_ids), paths in grouped_findings.items()
    )
    return AnnotationReplacementProposal(
        annotation=Annotation.model_validate(annotation_data),
        changed_paths=tuple(changed_paths),
        findings=findings,
        replacements=tuple(
            OntologyReplacement(term_id, replacement_id, tuple(paths))
            for (term_id, replacement_id), paths in replacement_paths.items()
        ),
    )


def _belongs_to_ontology(
    term_id: str,
    snapshot: OntologySnapshot,
    previous_terms: Mapping[str, OntologyTerm],
    definition: OntologyDefinition,
) -> bool:
    """Return whether a term identifier belongs to the ontology being loaded."""
    prefix, separator, _local_id = term_id.partition(":")
    return (
        term_id in snapshot.terms
        or term_id in previous_terms
        or (bool(separator) and prefix in definition.identifier_prefixes)
    )


def _set_term_path(annotation_data: dict[str, object], path: str, value: str) -> None:
    """Replace a primary or annotation-extension term at the given path."""
    if path == "/ontology_class_id":
        annotation_data["ontology_class_id"] = value
        return
    index = int(path.split("/")[2])
    extensions = annotation_data["annotation_extensions"]
    if not isinstance(extensions, list):
        raise TypeError("annotation extensions must be a list")
    extension = extensions[index]
    if not isinstance(extension, dict):
        raise TypeError("annotation extension must be an object")
    extension["extension_term"] = value


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
