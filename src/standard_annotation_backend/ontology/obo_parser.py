"""Convert OBO documents to SABE ontology snapshots with `fastobo`.

SABE reads only a few clauses from each term. Before parsing, it removes the rest,
so clauses SABE does not need cannot stop a published file from loading.
"""

from __future__ import annotations

from collections.abc import Mapping
from io import BytesIO
from types import MappingProxyType

import fastobo
from fastobo import header, term, typedef  # ty: ignore[unresolved-import]

from standard_annotation_backend.domain.ontology import (
    SUBCLASS_PREDICATE,
    OntologyDefinition,
    OntologyDocument,
    OntologyEdge,
    OntologyParseError,
    OntologySnapshot,
    OntologyTerm,
)

_TERM_TAGS = frozenset(
    {
        b"id",
        b"is_obsolete",
        b"replaced_by",
        b"consider",
        b"alt_id",
        b"subset",
        b"is_a",
        b"relationship",
    }
)
"""Clause tags that SABE reads from `[Term]` frames."""


def parse_obo(
    document: OntologyDocument,
    definition: OntologyDefinition,
) -> OntologySnapshot:
    """Parse an OBO document into the terms, edges, and metadata SABE stores.

    Only terms whose identifiers have one of the ontology's prefixes are kept, so a
    file that also contains terms from other ontologies still loads. Replacement
    suggestions are kept as published, even when they point to an obsolete term or
    into another ontology.

    Args:
        document: Immutable source bytes and retrieval metadata.
        definition: The ontology's configured prefixes, closure predicates, and
            expected `ontology:` header.

    Returns:
        The parsed terms, approved relation edges, alternative identifiers, and
        declared subsets.

    Raises:
        OntologyParseError: If the syntax is invalid, the file declares another
            ontology, it has no terms with the ontology's prefixes, a term is defined
            twice, a relation alias maps to more than one approved predicate, or a
            closure edge points to a term the file does not define.
    """
    if document.ontology_key != definition.key:
        raise OntologyParseError("ontology document key does not match its definition")

    try:
        reader = fastobo.iter(  # ty: ignore[unresolved-attribute]
            _without_unread_clauses(document.content),
            ordered=False,
            threads=1,
        )
        header_frame = reader.header()
        document_version = _document_version(header_frame)
        declared_ontology = _declared_ontology(header_frame)
        declared_subsets = _declared_subsets(header_frame)
        terms: dict[str, OntologyTerm] = {}
        alt_id_claims: dict[str, set[str]] = {}
        edges: set[OntologyEdge] = set()
        relationship_edges: set[OntologyEdge] = set()
        typedef_predicates: dict[str, set[str]] = {}
        duplicate_term_id: str | None = None
        for frame in reader:
            if isinstance(frame, typedef.TypedefFrame):
                configured_predicates = {
                    str(clause.xref.id)
                    for clause in frame
                    if isinstance(clause, typedef.XrefClause)
                    and str(clause.xref.id) in definition.closure_predicates
                }
                if configured_predicates:
                    typedef_predicates.setdefault(str(frame.id), set()).update(
                        configured_predicates
                    )
                continue
            if not isinstance(frame, term.TermFrame):
                continue
            term_id = str(frame.id)
            if not _has_owned_prefix(term_id, definition):
                continue
            if term_id in terms and duplicate_term_id is None:
                duplicate_term_id = term_id
            obsolete = False
            replaced_by: list[str] = []
            consider: list[str] = []
            subsets: list[str] = []
            for clause in frame:
                if isinstance(clause, term.IsObsoleteClause):
                    obsolete = clause.obsolete
                elif isinstance(clause, term.ReplacedByClause):
                    replaced_by.append(str(clause.term))
                elif isinstance(clause, term.ConsiderClause):
                    consider.append(str(clause.term))
                elif isinstance(clause, term.AltIdClause):
                    alt_id = str(clause.alt_id)
                    if _has_owned_prefix(alt_id, definition):
                        alt_id_claims.setdefault(alt_id, set()).add(term_id)
                elif isinstance(clause, term.SubsetClause):
                    subsets.append(str(clause.subset))
                elif (
                    isinstance(clause, term.IsAClause)
                    and SUBCLASS_PREDICATE in definition.closure_predicates
                ):
                    object_term_id = str(clause.term)
                    if _has_owned_prefix(object_term_id, definition):
                        edges.add(
                            OntologyEdge(
                                subject_term_id=term_id,
                                predicate_id=SUBCLASS_PREDICATE,
                                object_term_id=object_term_id,
                            )
                        )
                elif isinstance(clause, term.RelationshipClause):
                    object_term_id = str(clause.term)
                    if _has_owned_prefix(object_term_id, definition):
                        relationship_edges.add(
                            OntologyEdge(
                                subject_term_id=term_id,
                                predicate_id=str(clause.typedef),
                                object_term_id=object_term_id,
                            )
                        )
            terms[term_id] = OntologyTerm(
                term_id=term_id,
                obsolete=obsolete,
                replaced_by=tuple(sorted(set(replaced_by))),
                consider=tuple(sorted(set(consider))),
                subsets=tuple(sorted(set(subsets))),
            )
    except (OSError, SyntaxError, TypeError, ValueError) as error:
        raise OntologyParseError("ontology source is not valid OBO") from error

    edges.update(
        _resolve_relationship_edges(
            relationship_edges,
            typedef_predicates,
            definition,
        )
    )

    if declared_ontology != definition.document_ontology_id:
        raise OntologyParseError(
            f"ontology source declares ontology {declared_ontology or '<missing>'}; "
            f"expected {definition.document_ontology_id}"
        )
    if not terms:
        raise OntologyParseError(
            "ontology source contains no terms with the ontology's prefixes"
        )
    if duplicate_term_id is not None:
        raise OntologyParseError(
            f"ontology contains duplicate term {duplicate_term_id}"
        )
    _validate_edge_targets(terms, edges)
    return OntologySnapshot(
        document=document,
        document_version=document_version,
        closure_predicates=definition.closure_predicates,
        terms=MappingProxyType(terms),
        edges=tuple(sorted(edges)),
        alt_ids=MappingProxyType(_unambiguous_alt_ids(alt_id_claims, terms)),
        declared_subsets=declared_subsets,
        identifier_prefixes=definition.identifier_prefixes,
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


def _has_owned_prefix(
    identifier: str,
    definition: OntologyDefinition,
) -> bool:
    """Return whether an identifier's prefix is one that the ontology owns."""
    return identifier.partition(":")[0] in definition.identifier_prefixes


def _resolve_relationship_edges(
    edges: set[OntologyEdge],
    typedef_predicates: dict[str, set[str]],
    definition: OntologyDefinition,
) -> set[OntologyEdge]:
    """Return configured relationship edges with canonical predicate IDs."""
    ambiguous_alias = next(
        (
            alias
            for alias, predicates in sorted(typedef_predicates.items())
            if len(predicates) > 1
        ),
        None,
    )
    if ambiguous_alias is not None:
        raise OntologyParseError(
            f"ontology typedef {ambiguous_alias} maps to multiple closure predicates"
        )

    resolved: set[OntologyEdge] = set()
    for edge in edges:
        predicate_id = edge.predicate_id
        if predicate_id not in definition.closure_predicates:
            predicate_id = next(iter(typedef_predicates.get(predicate_id, ())), None)
        if predicate_id is not None:
            resolved.add(
                OntologyEdge(
                    subject_term_id=edge.subject_term_id,
                    predicate_id=predicate_id,
                    object_term_id=edge.object_term_id,
                )
            )
    return resolved


def _validate_edge_targets(
    terms: Mapping[str, OntologyTerm],
    edges: set[OntologyEdge],
) -> None:
    """Reject a closure edge whose target the document does not define.

    Closure would be incomplete without the target, so the whole document is
    rejected.
    """
    undefined = sorted({edge.object_term_id for edge in edges} - terms.keys())
    if undefined:
        raise OntologyParseError(f"ontology references undefined term {undefined[0]}")


def _without_unread_clauses(content: bytes) -> BytesIO:
    """Return the document, without the `[Term]` clauses SABE does not read.

    Published ontologies sometimes contain clauses that `fastobo` rejects, such as
    `xref:` URLs with backslash escapes in UBERON or `def:` references written
    `[PMID: 1]` in SO. SABE never reads those clauses, so removing them lets such
    files load and makes large files faster to parse. The header and other
    frames, such as `[Typedef]`, are kept unchanged. Lines are copied one at a
    time, so a large file is not held in memory twice as separate lines.

    Returns:
        A buffer positioned at the start of the reduced document.
    """
    kept = BytesIO()
    in_term_frame = False
    for line in BytesIO(content):
        stripped = line.strip()
        if stripped.startswith(b"["):
            in_term_frame = stripped == b"[Term]"
        elif (
            in_term_frame and stripped and stripped.partition(b":")[0] not in _TERM_TAGS
        ):
            continue
        kept.write(line)
    kept.seek(0)
    return kept


def _declared_subsets(frame: header.HeaderFrame) -> frozenset[str]:
    """Return the subset names the OBO header declares with `subsetdef`."""
    return frozenset(
        str(clause.subset)
        for clause in frame
        if isinstance(clause, header.SubsetdefClause)
    )


def _unambiguous_alt_ids(
    claims: Mapping[str, set[str]],
    terms: Mapping[str, OntologyTerm],
) -> dict[str, str]:
    """Return each alternative identifier that names exactly one term.

    An alternative identifier that is also a term's own identifier, or that more
    than one term claims, does not name one term, so it is left out.
    """
    return {
        alt_id: next(iter(claimants))
        for alt_id, claimants in sorted(claims.items())
        if len(claimants) == 1 and alt_id not in terms
    }
