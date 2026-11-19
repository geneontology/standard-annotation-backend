"""Define the facts that semantic validation and term replacement read.

A fact is a piece of information about an annotation that comes from outside it,
such as whether a term it uses is obsolete. A repository reads the facts from the
active reference data and the other active annotations. The checks read only these
values, so every check runs without a database.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from uuid import UUID

from standard_annotation_backend.domain.errors import UnavailableError
from standard_annotation_backend.domain.semantic_validation.rule_data import (
    RuleDataIndex,
)

RULES_REFERENCE_DATA = "rules"
"""The name `ReferenceDataUnavailableError` uses for the go-site rule data."""


@dataclass(frozen=True, slots=True)
class CurrentTerm:
    """Describe an identifier that is a current term of its ontology.

    Attributes:
        subsets: The subsets the term belongs to.
        checked_ancestors: The checked ancestors (see the `checked_ancestors`
            module) the term is, or descends from by `rdfs:subClassOf`. Filled
            in only for GO terms.
    """

    subsets: frozenset[str] = frozenset()
    checked_ancestors: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ObsoleteTerm:
    """Describe an identifier that is an obsolete term of its ontology.

    Attributes:
        replaced_by: The terms the ontology names as replacements.
        consider: The terms the ontology suggests considering instead.
    """

    replaced_by: tuple[str, ...] = ()
    consider: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AlternateId:
    """Describe an identifier that is an alternative ID of a term.

    Attributes:
        primary_id: The term the identifier is an alternative ID of.
    """

    primary_id: str


@dataclass(frozen=True, slots=True)
class UnknownId:
    """Describe an identifier that is neither a term nor an alternative ID."""


type TermFact = CurrentTerm | ObsoleteTerm | AlternateId | UnknownId
"""What the active snapshot of an identifier's ontology says about it.

The class says whether the identifier is a current term, an obsolete term, an
alternative ID, or none of these, and carries only the facts that apply.
"""


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
        rule_data: The active go-site rule data, indexed for the GO Rules.
        duplicate_peer_ids: Other active annotations that duplicate this one.
        retracted_pmids: The annotation's references that the active go-site
            rule data lists in `retracted-publications.txt`.
    """

    terms: Mapping[str, TermFact]
    entity: EntityFact | None
    rule_data: RuleDataIndex
    duplicate_peer_ids: tuple[UUID, ...] = ()
    retracted_pmids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ActiveSnapshots:
    """Identify the active snapshots that a set of facts is read from.

    A snapshot is one stored version of a reference data source. The snapshots are
    looked up once, before any facts are read, and stay locked against pruning
    until the transaction ends. Facts read together therefore never mix two
    versions of an ontology, the rule data, or an entity catalog, even if a
    refresh activates a newer snapshot meanwhile.

    Attributes:
        ontology_versions: Each configured ontology's active snapshot, keyed by
            ontology key.
        rules_snapshot_id: The active go-site rule data snapshot.
        rule_data: That snapshot's rule data, indexed for the GO Rules.
        entity_catalogs: Each active entity catalog's snapshot, keyed by source
            key.
    """

    ontology_versions: Mapping[str, UUID]
    rules_snapshot_id: UUID
    rule_data: RuleDataIndex
    entity_catalogs: Mapping[str, UUID] = field(default_factory=dict)

    @property
    def snapshot_ids(self) -> frozenset[UUID]:
        """Return the ID of every snapshot these facts are read from."""
        return frozenset(
            {
                *self.ontology_versions.values(),
                self.rules_snapshot_id,
                *self.entity_catalogs.values(),
            }
        )


class ReferenceDataUnavailableError(UnavailableError):
    """Report reference data that validation needs but cannot read.

    Every configured ontology and the go-site rule data must have an active
    snapshot before SABE can validate annotations. The rule data is also
    unavailable when an earlier release stored it in a form this release cannot
    read; the next rules refresh stores it again.

    Attributes:
        missing: The ontology keys, and `rules` for the go-site rule data, that
            are unavailable.
    """

    code = "reference_data_unavailable"
    message = "Reference data needed to validate annotations has not been loaded"

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing = tuple(sorted(missing))
        super().__init__()
