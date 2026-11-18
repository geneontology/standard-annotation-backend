"""Define the vocabulary shared by every stored snapshot of reference data.

A snapshot is one stored version of an ontology, of the go-site files that GO
Rules read, or of an entity catalog. `persistence.repositories.snapshots` stores
and reads snapshots and describes how they behave. The values here need neither
HTTP nor a database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from standard_annotation_backend.domain.refresh import (
    RefreshKindName,
    SourceProvenance,
)


class SnapshotKind(StrEnum):
    """Name each kind of reference data that SABE stores as versioned snapshots.

    Authorization and annotations are refreshed too, but SABE stores them as
    current state, not as snapshots.
    """

    ONTOLOGY = "ontology"
    RULES = "rules"
    ENTITY = "entity"


READER_VERSIONS: Mapping[RefreshKindName, int] = MappingProxyType(
    {
        RefreshKindName.AUTHORIZATION: 1,
        RefreshKindName.ONTOLOGY: 1,
        RefreshKindName.RULES: 1,
        RefreshKindName.ENTITY: 2,
    }
)
"""The version of the code that reads each kind's source documents.

Every refresh records the version it read its document with. Increase a kind's
version whenever its parser, or the way its parsed data is stored, changes: the
next refresh of each source of that kind then reads its document again even
when the document is unchanged. An upgrade of the schema package needs no new
version: entity refreshes also compare the installed schema package version (see
`EntitySnapshotDetail.reusable`), and annotation refreshes compare it instead of a
reader version.
"""


def current_reader_version(kind: RefreshKindName) -> int:
    """Return the reader version this release records for `kind`.

    Raises:
        KeyError: If `kind` has no reader version, as for annotations.
    """
    return READER_VERSIONS[kind]


@dataclass(frozen=True, slots=True)
class SourceContent:
    """Identify what a refresh stored, to decide whether a new document is the same.

    A refresh skips a document whose content equals that of the current state.
    The source revision is not part of the content, so a new commit with
    identical bytes is the same content. An entity refresh still reads a document
    with the same content when the active snapshot's rows were checked with another
    schema package version or its activation rejected a row as a collision (see
    `EntitySnapshotDetail.reusable`).

    Attributes:
        source_type: `github` or `https`.
        source_locator: Where the document came from.
        source_checksum: SHA-256 of the downloaded bytes.
        reader_version: The reader version the document was, or would be, read
            with.
        identifier_prefixes: For an ontology, the prefixes it owned when read,
            sorted. Empty for other kinds.
        closure_predicates: For an ontology, the predicates whose closure was
            computed, sorted. Empty for other kinds.
    """

    source_type: str
    source_locator: str
    source_checksum: str
    reader_version: int
    identifier_prefixes: tuple[str, ...] = ()
    closure_predicates: tuple[str, ...] = ()

    @classmethod
    def of(
        cls,
        kind: RefreshKindName,
        provenance: SourceProvenance,
        *,
        identifier_prefixes: Iterable[str] = (),
        closure_predicates: Iterable[str] = (),
    ) -> SourceContent:
        """Return the content this release would store for a fetched document."""
        return cls(
            source_type=provenance.source_type,
            source_locator=provenance.source_locator,
            source_checksum=provenance.source_checksum,
            reader_version=current_reader_version(kind),
            identifier_prefixes=tuple(sorted(identifier_prefixes)),
            closure_predicates=tuple(sorted(closure_predicates)),
        )
