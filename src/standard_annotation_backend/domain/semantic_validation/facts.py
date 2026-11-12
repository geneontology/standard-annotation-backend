"""Hold what semantic validation knows about one annotation.

A repository gathers these facts from the active reference data and the other
active annotations. The checks read only the facts, so every check runs without a
database.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from standard_annotation_backend.domain.errors import UnavailableError
from standard_annotation_backend.domain.semantic_validation.rule_data import RuleData


class TermStatus(StrEnum):
    """Say what an identifier is in the ontology that owns its prefix."""

    CURRENT = "current"
    OBSOLETE = "obsolete"
    ALTERNATE = "alternate"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TermFact:
    """Describe one identifier in the active snapshot of its ontology.

    Attributes:
        status: Whether the identifier is a current term, an obsolete term, an
            alternative identifier of a term, or none of these.
        replaced_by: The terms the ontology names as replacements.
        consider: The terms the ontology suggests considering instead.
        primary_id: For an alternative identifier, the term it names.
        subsets: The subsets the term belongs to.
        checked_ancestors: The checked ancestors (see the `checked_ancestors`
            module) the term is, or descends from by `rdfs:subClassOf`. Gathered
            only for current GO terms.
    """

    status: TermStatus
    replaced_by: tuple[str, ...] = ()
    consider: tuple[str, ...] = ()
    primary_id: str | None = None
    subsets: frozenset[str] = frozenset()
    checked_ancestors: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class EntityFact:
    """Describe an annotation subject that an entity catalog lists as active.

    Attributes:
        db_object_taxon_id: The entity's taxon, from its GPI record.
        db_object_type: The entity's type, from its GPI record.
        type_checked_ancestors: The checked ancestors the entity's type falls under.
            Empty unless the type is a GO term.
    """

    db_object_taxon_id: str
    db_object_type: str
    type_checked_ancestors: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AnnotationFacts:
    """Hold every fact the checks need about one annotation.

    Attributes:
        terms: A fact for each identifier whose prefix a configured ontology owns.
            Identifiers with other prefixes have no fact and are not checked.
        entity: The subject's entity, or `None` if no entity catalog lists it.
        duplicate_peer_ids: Other active annotations that duplicate this one.
        rule_data: The active go-site rule data. `None` only when facts are built
            by hand without it; rules that need it then do not fire.
        retracted_pmids: The annotation's references that are listed as
            retracted.
    """

    terms: Mapping[str, TermFact]
    entity: EntityFact | None
    duplicate_peer_ids: tuple[UUID, ...] = ()
    rule_data: RuleData | None = None
    retracted_pmids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ReplacementFacts:
    """Hold what term replacement knows about a batch of annotations.

    Attributes:
        terms: A fact for each identifier the annotations use whose prefix a
            configured ontology owns, and for each term their replacement chains
            reach.
        snapshot_ids: The snapshot ID read for each ontology key and for `rules`.
    """

    terms: Mapping[str, TermFact]
    snapshot_ids: Mapping[str, UUID]


class ReferenceDataUnavailableError(UnavailableError):
    """Report reference data that validation needs but that was never loaded.

    Every configured ontology and the go-site rule data must have an active
    snapshot before SABE can validate a write.

    Attributes:
        missing: The ontology keys, and `rules` for the go-site rule data, that
            have no active snapshot.
    """

    code = "reference_data_unavailable"
    message = "Reference data needed to validate annotations has not been loaded"

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing = tuple(sorted(missing))
        super().__init__()
