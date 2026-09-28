"""Convert OBO documents to SAB ontology snapshots with `fastobo`."""

from __future__ import annotations

from io import BytesIO
from types import MappingProxyType

import fastobo
from fastobo import header, term  # ty: ignore[unresolved-import]

from standard_annotation_backend.domain.ontology import (
    OntologyDefinition,
    OntologyDocument,
    OntologyEdge,
    OntologyParseError,
    OntologySnapshot,
    OntologyTerm,
)

_SUBCLASS_PREDICATE = "rdfs:subClassOf"


def parse_obo(
    document: OntologyDocument,
    definition: OntologyDefinition,
) -> OntologySnapshot:
    """Parse a complete OBO document into terms and approved direct edges.

    Args:
        document: Immutable source bytes and retrieval metadata.
        definition: Ontology-specific identifiers and closure predicates.

    Returns:
        The parsed terms, replacement metadata, and approved relation edges.

    Raises:
        OntologyParseError: If syntax is invalid or referenced terms are undefined.
    """
    if document.ontology_key is not definition.key:
        raise OntologyParseError("ontology document key does not match its definition")

    try:
        reader = fastobo.iter(  # ty: ignore[unresolved-attribute]
            BytesIO(document.content), ordered=False, threads=1
        )
        header_frame = reader.header()
        document_version = _document_version(header_frame)
        declared_ontology = _declared_ontology(header_frame)
        terms: dict[str, OntologyTerm] = {}
        edges: set[OntologyEdge] = set()
        duplicate_term_id: str | None = None
        for frame in reader:
            if not isinstance(frame, term.TermFrame):
                continue
            term_id = str(frame.id)
            if term_id in terms and duplicate_term_id is None:
                duplicate_term_id = term_id
            obsolete = False
            replaced_by: list[str] = []
            consider: list[str] = []
            for clause in frame:
                if isinstance(clause, term.IsObsoleteClause):
                    obsolete = clause.obsolete
                elif isinstance(clause, term.ReplacedByClause):
                    replaced_by.append(str(clause.term))
                elif isinstance(clause, term.ConsiderClause):
                    consider.append(str(clause.term))
                elif (
                    isinstance(clause, term.IsAClause)
                    and _SUBCLASS_PREDICATE in definition.closure_predicates
                ):
                    edges.add(
                        OntologyEdge(
                            subject_term_id=term_id,
                            predicate_id=_SUBCLASS_PREDICATE,
                            object_term_id=str(clause.term),
                        )
                    )
                elif isinstance(clause, term.RelationshipClause):
                    predicate_id = str(clause.typedef)
                    if predicate_id in definition.closure_predicates:
                        edges.add(
                            OntologyEdge(
                                subject_term_id=term_id,
                                predicate_id=predicate_id,
                                object_term_id=str(clause.term),
                            )
                        )
            terms[term_id] = OntologyTerm(
                term_id=term_id,
                obsolete=obsolete,
                replaced_by=tuple(sorted(set(replaced_by))),
                consider=tuple(sorted(set(consider))),
            )
    except (OSError, SyntaxError, TypeError, ValueError) as error:
        raise OntologyParseError("ontology source is not valid OBO") from error

    expected_ontology = definition.document_ontology_id or definition.key.value
    if declared_ontology != expected_ontology:
        raise OntologyParseError(
            f"ontology source declares ontology {declared_ontology or '<missing>'}; "
            f"expected {expected_ontology}"
        )
    if not terms:
        raise OntologyParseError("ontology source contains no terms")
    if duplicate_term_id is not None:
        raise OntologyParseError(
            f"ontology contains duplicate term {duplicate_term_id}"
        )
    unexpected = next(
        (
            term_id
            for term_id in sorted(terms)
            if term_id.partition(":")[0] not in definition.identifier_prefixes
        ),
        None,
    )
    if unexpected is not None:
        raise OntologyParseError(
            f"ontology contains unexpected identifier {unexpected}"
        )
    _validate_references(terms, edges)
    return OntologySnapshot(
        document=document,
        document_version=document_version,
        closure_predicates=definition.closure_predicates,
        terms=MappingProxyType(terms),
        edges=tuple(sorted(edges)),
    )


def _document_version(frame: header.HeaderFrame) -> str | None:
    """Return the declared OBO data version when one is present."""
    return next(
        (
            str(clause.version)
            for clause in frame
            if isinstance(clause, header.DataVersionClause)
        ),
        None,
    )


def _declared_ontology(frame: header.HeaderFrame) -> str | None:
    """Return the OBO ontology identity when one is declared."""
    return next(
        (
            str(clause.ontology)
            for clause in frame
            if isinstance(clause, header.OntologyClause)
        ),
        None,
    )


def _validate_references(
    terms: dict[str, OntologyTerm],
    edges: set[OntologyEdge],
) -> None:
    """Reject edges and replacement metadata that target undefined terms."""
    targets = {edge.object_term_id for edge in edges} | {
        target
        for ontology_term in terms.values()
        for target in (*ontology_term.replaced_by, *ontology_term.consider)
    }
    undefined = sorted(targets - terms.keys())
    if undefined:
        raise OntologyParseError(f"ontology references undefined term {undefined[0]}")
    obsolete_replacements = sorted(
        target
        for ontology_term in terms.values()
        for target in ontology_term.replaced_by
        if terms[target].obsolete
    )
    if obsolete_replacements:
        raise OntologyParseError(
            f"ontology replacement target {obsolete_replacements[0]} is obsolete"
        )
