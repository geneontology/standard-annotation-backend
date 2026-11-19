"""Define the go-site metadata that GO Rules check annotations against.

The `rules` refresh reads six go-site files. Five of them are kept here, as
`RuleData`: databases and their identifier patterns (`db-xrefs.yaml`), GO_REF
references (`gorefs.yaml`), group shorthands (`groups.yaml`), evidence usage
constraints (`eco-usage-constraints.yaml`), and extension constraints
(`extensions-constraints.yaml`). The sixth, `retracted-publications.txt`, lists
about 26,000 PMIDs and is stored as database rows instead.

Only the values the rules use are kept, and they were validated when the files
were read, so the rules can rely on them without checking again.
"""

import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar, Literal, Self

from standard_annotation_backend.domain.stored_json import StoredJson


class WithPresence(StrEnum):
    """State whether annotations with an evidence term must have `with_or_from` values.

    `mandatory` requires values and `disallowed` forbids them. `optional` and
    `recommended` impose no requirement. `conditional` chooses one of the others
    from a `WithPresenceCondition`.
    """

    MANDATORY = "mandatory"
    DISALLOWED = "disallowed"
    OPTIONAL = "optional"
    RECOMMENDED = "recommended"
    CONDITIONAL = "conditional"


@dataclass(frozen=True, slots=True)
class DatabaseEntry:
    """Describe one database listed in `db-xrefs.yaml`.

    Attributes:
        database: The prefix of the database's identifiers, such as `MGI`. An
            identifier's prefix must equal it exactly, including case.
        id_syntaxes: Patterns from the database's entity types. When there are any,
            a local identifier must fully match at least one of them.
    """

    database: str
    id_syntaxes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GoRefEntry:
    """Describe one GO_REF reference listed in `gorefs.yaml`.

    Attributes:
        go_ref_id: The reference's identifier, such as `GO_REF:0000033`.
        obsolete: Whether the reference is marked obsolete.
        alt_ids: Older identifiers that now name this reference.
    """

    go_ref_id: str
    obsolete: bool = False
    alt_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntityIsComplex:
    """Test whether the entity's type is `GO:0032991` or one of its descendants.

    `GO:0032991` is protein-containing complex.

    `eco-usage-constraints.yaml` writes this condition as
    `db_object_type eq 'complex'`.
    """

    kind: Literal["entity_is_complex"] = "entity_is_complex"


@dataclass(frozen=True, slots=True)
class ReferenceMatches:
    """Test whether any of the annotation's references matches `pattern`.

    `eco-usage-constraints.yaml` writes this condition as
    `reference matches '<pattern>'`.
    """

    pattern: str
    kind: Literal["reference_matches"] = "reference_matches"


@dataclass(frozen=True, slots=True)
class WithPresenceCondition:
    """Choose an evidence term's `with_or_from` requirement from a condition.

    Attributes:
        test: The condition to evaluate on the annotation.
        when_true: The requirement when the condition holds. Never `conditional`.
        when_false: The requirement otherwise. Never `conditional`.
    """

    test: EntityIsComplex | ReferenceMatches
    when_true: WithPresence
    when_false: WithPresence


@dataclass(frozen=True, slots=True)
class EcoUsageConstraint:
    """Describe what annotations with one evidence term must cite and include.

    Attributes:
        eco_id: The evidence term the constraint applies to.
        allowed_reference: A pattern that at least one reference must fully match,
            or `None` when any reference is allowed.
        with_presence: Whether `with_or_from` must be present, must be absent, or
            may be either.
        condition: How to choose the requirement when `with_presence` is
            `conditional`; `None` otherwise.
    """

    eco_id: str
    allowed_reference: str | None
    with_presence: WithPresence
    condition: WithPresenceCondition | None = None


@dataclass(frozen=True, slots=True)
class ExtensionConstraint:
    """Describe one entry of `extensions-constraints.yaml`.

    The file may list several entries for one relation. An extension conforms if
    it meets any one of them.

    Attributes:
        relation_id: The extension relation as a prefixed identifier (CURIE) in
            `RO`, `BFO`, or `GOREL`, such as `BFO:0000066`. Annotations name
            relations the same way.
        namespaces: Identifier prefixes allowed for the extension term.
        primary_root_terms: GO terms the annotated term must be, or descend from.
        cardinality: The most extensions with this relation and term prefix that
            one annotation may have, or `None` for no limit.
    """

    relation_id: str
    namespaces: tuple[str, ...]
    primary_root_terms: tuple[str, ...]
    cardinality: int | None = None


@dataclass(frozen=True, slots=True)
class RuleData:
    """Hold the validated go-site metadata from one refresh.

    Attributes:
        databases: Every database in `db-xrefs.yaml`, each listed once.
        go_refs: Every reference in `gorefs.yaml`, each listed once.
        group_shorthands: The `shorthand` of every group in `groups.yaml`, which
            are the values `assigned_by` may take.
        eco_usage_constraints: Every entry of `eco-usage-constraints.yaml`, at most
            one per evidence term.
        extension_constraints: Every entry of `extensions-constraints.yaml` that
            has a `relation_id`. Entries without one cannot match an annotation's
            relation, so they are left out.
    """

    databases: tuple[DatabaseEntry, ...]
    go_refs: tuple[GoRefEntry, ...]
    group_shorthands: tuple[str, ...]
    eco_usage_constraints: tuple[EcoUsageConstraint, ...]
    extension_constraints: tuple[ExtensionConstraint, ...]


@dataclass(frozen=True, slots=True)
class RuleDataIndex:
    """Hold rule data with the lookups and compiled patterns that GO Rules use.

    GO Rules check every annotation against the same rule data, so the lookups are
    built and the patterns compiled once, when a snapshot's rule data is first
    read, instead of for each annotation. Build one with `RuleDataIndex.of`.

    One index is cached per process and shared by every validation that reads the
    same snapshot, so its lookups are read-only: writing to one raises
    `TypeError`. The index is not hashable.

    Attributes:
        data: The rule data the index was built from.
        databases: Each database entry, by its `database` prefix.
        group_shorthands: Every group shorthand.
        go_refs: Each GO_REF entry, by its identifier.
        primary_go_ref_ids: The primary identifier of each GO_REF alternative ID.
        eco_usage_constraints: Each evidence usage constraint, by its `eco_id`.
        extension_constraints: The extension constraints for each `relation_id`, in
            file order.
        patterns: Every `id_syntax`, `allowed_reference`, and `reference matches`
            pattern in the data, compiled, by its text.
    """

    data: RuleData
    databases: Mapping[str, DatabaseEntry]
    group_shorthands: frozenset[str]
    go_refs: Mapping[str, GoRefEntry]
    primary_go_ref_ids: Mapping[str, str]
    eco_usage_constraints: Mapping[str, EcoUsageConstraint]
    extension_constraints: Mapping[str, tuple[ExtensionConstraint, ...]]
    patterns: Mapping[str, re.Pattern[str]]

    # Hashing would fail on the read-only mappings; say so plainly instead.
    __hash__: ClassVar[None] = None

    @classmethod
    def of(cls, data: RuleData) -> Self:
        """Return the index of `data`.

        Raises:
            re.error: If a pattern does not compile. A rules refresh rejects such
                files, so stored rule data never has one.
        """
        extension_constraints: dict[str, list[ExtensionConstraint]] = {}
        for constraint in data.extension_constraints:
            extension_constraints.setdefault(constraint.relation_id, []).append(
                constraint
            )
        return cls(
            data=data,
            databases=MappingProxyType(
                {entry.database: entry for entry in data.databases}
            ),
            group_shorthands=frozenset(data.group_shorthands),
            go_refs=MappingProxyType(
                {entry.go_ref_id: entry for entry in data.go_refs}
            ),
            primary_go_ref_ids=MappingProxyType(
                {
                    alt_id: entry.go_ref_id
                    for entry in data.go_refs
                    for alt_id in entry.alt_ids
                }
            ),
            eco_usage_constraints=MappingProxyType(
                {entry.eco_id: entry for entry in data.eco_usage_constraints}
            ),
            extension_constraints=MappingProxyType(
                {
                    relation_id: tuple(constraints)
                    for relation_id, constraints in extension_constraints.items()
                }
            ),
            patterns=MappingProxyType(
                {pattern: re.compile(pattern) for pattern in _patterns(data)}
            ),
        )

    def fullmatch(self, pattern: str, value: str) -> bool:
        """Return whether all of `value` matches `pattern`, a pattern in the data."""
        return self.patterns[pattern].fullmatch(value) is not None


def _patterns(data: RuleData) -> Iterator[str]:
    """Yield every pattern in `data`, possibly more than once."""
    for entry in data.databases:
        yield from entry.id_syntaxes
    for constraint in data.eco_usage_constraints:
        if constraint.allowed_reference is not None:
            yield constraint.allowed_reference
        if constraint.condition is not None and isinstance(
            constraint.condition.test, ReferenceMatches
        ):
            yield constraint.condition.test.pattern


STORED_RULE_DATA = StoredJson(RuleData, label="rule data")
"""The JSON form of `RuleData` stored with each rules snapshot."""


def unrecognized_extension_namespaces(
    data: RuleData, ontology_prefixes: Collection[str]
) -> tuple[str, ...]:
    """Return extension namespaces that are neither an ontology prefix nor a database.

    `extensions-constraints.yaml` lists the identifier prefixes an extension term
    may use. SABE recognizes a prefix if a configured ontology owns it or
    `db-xrefs.yaml` lists it as a database. A rules refresh lists the namespaces
    returned here in its result.

    Args:
        data: The refreshed go-site metadata.
        ontology_prefixes: Identifier prefixes owned by configured ontologies.

    Returns:
        The unrecognized namespaces, sorted, each listed once.
    """
    databases = {entry.database for entry in data.databases}
    return tuple(
        sorted(
            {
                namespace
                for constraint in data.extension_constraints
                for namespace in constraint.namespaces
                if namespace not in ontology_prefixes and namespace not in databases
            }
        )
    )
