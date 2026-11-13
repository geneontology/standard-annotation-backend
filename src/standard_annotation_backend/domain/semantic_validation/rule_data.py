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

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

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
