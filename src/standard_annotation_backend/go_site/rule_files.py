"""Parse the go-site metadata files that GO Rules read.

The `rules` refresh reads six files from one commit of the go-site repository.
Each parser below validates one file and keeps only the values the rules use.
Fields SABE does not read are ignored, so a new field added upstream does not make
a refresh fail. The values the rules do read are checked strictly: a malformed
identifier, a pattern Python cannot compile, or an entry that repeats an earlier
one makes the file invalid. The refresh then fails, and the rule data stored by
the last successful refresh stays in use.

Issues give the location of each problem within its file, never the rejected
value.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

from standard_annotation_backend.domain.semantic_validation.rule_data import (
    DatabaseEntry,
    EcoUsageConstraint,
    EntityIsComplex,
    ExtensionConstraint,
    GoRefEntry,
    ReferenceMatches,
    RuleData,
    WithPresence,
    WithPresenceCondition,
)
from standard_annotation_backend.domain.validation import (
    ValidationIssue,
    validation_issues,
)
from standard_annotation_backend.yaml_loading import load_yaml

DB_XREFS_PATH = "metadata/db-xrefs.yaml"
GOREFS_PATH = "metadata/gorefs.yaml"
GROUPS_PATH = "metadata/groups.yaml"
RETRACTED_PUBLICATIONS_PATH = "metadata/retracted-publications.txt"
ECO_USAGE_CONSTRAINTS_PATH = "metadata/eco-usage-constraints.yaml"
EXTENSIONS_CONSTRAINTS_PATH = "metadata/extensions-constraints.yaml"
RULE_FILE_PATHS = (
    DB_XREFS_PATH,
    GOREFS_PATH,
    GROUPS_PATH,
    RETRACTED_PUBLICATIONS_PATH,
    ECO_USAGE_CONSTRAINTS_PATH,
    EXTENSIONS_CONSTRAINTS_PATH,
)
"""Every file the `rules` refresh reads, as paths in the go-site repository."""

_PMID = re.compile(r"PMID:[0-9]+")
_ENTITY_IS_COMPLEX = re.compile(r"db_object_type\s+eq\s+'complex'")
_REFERENCE_MATCHES = re.compile(r"reference\s+matches\s+'(.*)'")


class InvalidRuleFileError(Exception):
    """Report why go-site metadata files are invalid, without their contents.

    Attributes:
        issues: One issue per problem. The single-file parsers give locations
            within their file; `parse_rule_files` puts the file's path first.
    """

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        super().__init__("invalid go-site metadata file")
        self.issues = issues


@dataclass(frozen=True, slots=True)
class RuleFiles:
    """Hold the parsed contents of all six files.

    Attributes:
        data: The contents of every file except `retracted-publications.txt`.
        retracted_pmids: Every PMID that `retracted-publications.txt` lists.
    """

    data: RuleData
    retracted_pmids: frozenset[str]


def _none_as_empty(value: object) -> object:
    """Read a list field written with no value as an empty list."""
    return () if value is None else value


def _compiled(value: str) -> str:
    """Require a pattern that Python's `re` module can compile."""
    try:
        re.compile(value)
    except re.error:
        raise PydanticCustomError(
            "invalid_pattern", "must be a valid regular expression"
        ) from None
    return value


def _condition(value: object) -> EntityIsComplex | ReferenceMatches:
    """Read one of the two conditions that GORULE:0000043 can evaluate.

    Any other condition makes the file invalid instead of being skipped, because
    the rule could not tell which `with_or_from` requirement applies.
    """
    if isinstance(value, str):
        text = value.strip()
        if _ENTITY_IS_COMPLEX.fullmatch(text):
            return EntityIsComplex()
        match = _REFERENCE_MATCHES.fullmatch(text)
        if match is not None:
            return ReferenceMatches(pattern=_compiled(match.group(1)))
    raise PydanticCustomError(
        "unsupported_condition",
        "must be db_object_type eq 'complex' or reference matches '<pattern>'",
    )


_Prefix = Annotated[str, StringConstraints(pattern=r"^[^\s:]+$")]
"""A database or namespace name: the text an identifier has before its first `:`."""

_Token = Annotated[str, StringConstraints(pattern=r"^\S+$")]
"""A name with no whitespace, such as a group shorthand or an extension relation."""

_Pattern = Annotated[str, AfterValidator(_compiled)]
_GoRefId = Annotated[str, StringConstraints(pattern=r"^GO_REF:[0-9]{7}$")]
_GoTermId = Annotated[str, StringConstraints(pattern=r"^GO:[0-9]{7}$")]
_EcoId = Annotated[str, StringConstraints(pattern=r"^ECO:[0-9]{7}$")]
_RelationId = Annotated[str, StringConstraints(pattern=r"^(RO|BFO|GOREL):[0-9]{7}$")]
"""A relation ID such as `RO:0002233`.

The pattern matches the one in go-site's `extensions-constraints.schema.yaml`.
"""


class _Entry(BaseModel):
    """Read one entry, ignoring the fields SABE does not use."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class _EntityType(_Entry):
    """Read an entity type's identifier pattern from `db-xrefs.yaml`."""

    id_syntax: _Pattern | None = None


class _Database(_Entry):
    """Read one database from `db-xrefs.yaml`."""

    database: _Prefix
    entity_types: Annotated[
        tuple[_EntityType, ...], BeforeValidator(_none_as_empty)
    ] = ()


class _DatabaseFile(RootModel[tuple[_Database, ...]]):
    """Read `db-xrefs.yaml`, a list of databases."""


class _GoRef(_Entry):
    """Read one reference from `gorefs.yaml`."""

    id: _GoRefId
    is_obsolete: bool | None = None
    alt_id: Annotated[tuple[_GoRefId, ...], BeforeValidator(_none_as_empty)] = ()


class _GoRefFile(RootModel[tuple[_GoRef, ...]]):
    """Read `gorefs.yaml`, a list of references."""


class _Group(_Entry):
    """Read one group from `groups.yaml`."""

    shorthand: _Token


class _GroupFile(RootModel[tuple[_Group, ...]]):
    """Read `groups.yaml`, a list of groups."""


class _Condition(_Entry):
    """Read the `with_presence_condition` of an evidence entry."""

    condition: Annotated[
        EntityIsComplex | ReferenceMatches, BeforeValidator(_condition)
    ]
    when_true: WithPresence
    when_false: WithPresence

    @model_validator(mode="after")
    def require_plain_outcomes(self) -> Self:
        """Reject an outcome that is itself conditional, since nothing decides it."""
        if WithPresence.CONDITIONAL in (self.when_true, self.when_false):
            raise PydanticCustomError(
                "nested_condition", "when_true and when_false must not be conditional"
            )
        return self


class _EcoConstraint(_Entry):
    """Read one entry of `eco-usage-constraints.yaml`."""

    eco_id: _EcoId
    allowed_reference: _Pattern | None = None
    with_presence: WithPresence
    with_presence_condition: _Condition | None = None

    @model_validator(mode="after")
    def require_condition_exactly_when_conditional(self) -> Self:
        """Require a condition for `conditional` and reject one anywhere else."""
        conditional = self.with_presence is WithPresence.CONDITIONAL
        if conditional != (self.with_presence_condition is not None):
            raise PydanticCustomError(
                "condition_mismatch",
                "with_presence_condition is required when with_presence is "
                "conditional and not allowed otherwise",
            )
        return self


class _EcoFile(_Entry):
    """Read `eco-usage-constraints.yaml`.

    The file's top-level `entity_types` list only defines YAML anchors that the
    entries refer to, so it is not read.
    """

    eco_usage_constraints: tuple[_EcoConstraint, ...]


class _ExtensionConstraint(_Entry):
    """Read one entry of `extensions-constraints.yaml`.

    The `relation` field, a relation name as written in GAF files, is not read,
    because annotations name relations by ID.
    """

    relation_id: _RelationId | None = None
    namespaces: Annotated[tuple[_Prefix, ...], Field(min_length=1)]
    primary_root_terms: Annotated[tuple[_GoTermId, ...], Field(min_length=1)]
    cardinality: Annotated[int, Field(strict=True, ge=1)] | None = None


class _ExtensionFile(RootModel[tuple[_ExtensionConstraint, ...]]):
    """Read `extensions-constraints.yaml`, a list of entries."""


def _validated[M: BaseModel](model: type[M], text: str) -> M:
    """Load a YAML document and validate it as `model`.

    Raises:
        InvalidRuleFileError: If the YAML is invalid or does not match `model`.
    """
    try:
        payload = load_yaml(text)
    except yaml.YAMLError:
        raise InvalidRuleFileError(
            (
                ValidationIssue(
                    location=(), message="invalid YAML document", type="invalid_yaml"
                ),
            )
        ) from None
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise InvalidRuleFileError(validation_issues(error)) from None


def _require_unique(
    values: Sequence[str], location: Callable[[int], tuple[str | int, ...]]
) -> None:
    """Reject entries whose key repeats an earlier entry's.

    Args:
        values: Each entry's key, in file order.
        location: Builds the location of the entry at an index.

    Raises:
        InvalidRuleFileError: With one issue for each repeated key.
    """
    seen: set[str] = set()
    issues: list[ValidationIssue] = []
    for index, value in enumerate(values):
        if value in seen:
            issues.append(
                ValidationIssue(
                    location=location(index),
                    message="must not repeat an earlier entry",
                    type="duplicate_entry",
                )
            )
        seen.add(value)
    if issues:
        raise InvalidRuleFileError(tuple(issues))


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    """Return values without repeats, keeping the first occurrence of each."""
    return tuple(dict.fromkeys(values))


def parse_db_xrefs(text: str) -> tuple[DatabaseEntry, ...]:
    """Parse `db-xrefs.yaml`: databases and the identifier patterns of each.

    Raises:
        InvalidRuleFileError: If the file is invalid or names a database twice.
    """
    entries = _validated(_DatabaseFile, text).root
    _require_unique(
        [entry.database for entry in entries], lambda index: (index, "database")
    )
    return tuple(
        DatabaseEntry(database=entry.database, id_syntaxes=_id_syntaxes(entry))
        for entry in entries
    )


def _id_syntaxes(entry: _Database) -> tuple[str, ...]:
    """Return the identifier patterns of a database's entity types, each once."""
    syntaxes = [entity_type.id_syntax for entity_type in entry.entity_types]
    return _unique(syntax for syntax in syntaxes if syntax is not None)


def parse_gorefs(text: str) -> tuple[GoRefEntry, ...]:
    """Parse `gorefs.yaml`: each GO_REF, whether it is obsolete, and its alt IDs.

    A reference without `is_obsolete` is current.

    Raises:
        InvalidRuleFileError: If the file is invalid or lists a reference twice.
    """
    entries = _validated(_GoRefFile, text).root
    _require_unique([entry.id for entry in entries], lambda index: (index, "id"))
    return tuple(
        GoRefEntry(
            go_ref_id=entry.id,
            obsolete=bool(entry.is_obsolete),
            alt_ids=_unique(entry.alt_id),
        )
        for entry in entries
    )


def parse_groups(text: str) -> tuple[str, ...]:
    """Parse `groups.yaml` into the shorthand of each group, in file order.

    Raises:
        InvalidRuleFileError: If the file is invalid or repeats a shorthand.
    """
    shorthands = [entry.shorthand for entry in _validated(_GroupFile, text).root]
    _require_unique(shorthands, lambda index: (index, "shorthand"))
    return tuple(shorthands)


def parse_retracted_publications(text: str) -> frozenset[str]:
    """Parse `retracted-publications.txt`: one retracted publication per line.

    Lines starting with `!` are comments, and blank lines are skipped. Every other
    line starts with a PMID, such as `PMID:10026173`. Anything after a comma that
    follows it, such as a PMCID, is ignored.

    Raises:
        InvalidRuleFileError: With the line number of each line that does not
            start with a PMID.
    """
    pmids: set[str] = set()
    issues: list[ValidationIssue] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("!"):
            continue
        pmid = stripped.split(",", 1)[0].strip()
        if _PMID.fullmatch(pmid) is None:
            issues.append(
                ValidationIssue(
                    location=(line_number,),
                    message="must start with a PMID such as PMID:12345",
                    type="invalid_pmid",
                )
            )
        else:
            pmids.add(pmid)
    if issues:
        raise InvalidRuleFileError(tuple(issues))
    return frozenset(pmids)


def parse_eco_usage_constraints(text: str) -> tuple[EcoUsageConstraint, ...]:
    """Parse `eco-usage-constraints.yaml`: what each evidence term requires.

    Raises:
        InvalidRuleFileError: If the file is invalid, has a condition other than
            the two that GORULE:0000043 can evaluate, or lists an evidence term
            twice.
    """
    entries = _validated(_EcoFile, text).eco_usage_constraints
    _require_unique(
        [entry.eco_id for entry in entries],
        lambda index: ("eco_usage_constraints", index, "eco_id"),
    )
    return tuple(
        EcoUsageConstraint(
            eco_id=entry.eco_id,
            allowed_reference=entry.allowed_reference,
            with_presence=entry.with_presence,
            condition=None
            if entry.with_presence_condition is None
            else WithPresenceCondition(
                test=entry.with_presence_condition.condition,
                when_true=entry.with_presence_condition.when_true,
                when_false=entry.with_presence_condition.when_false,
            ),
        )
        for entry in entries
    )


def parse_extensions_constraints(text: str) -> tuple[ExtensionConstraint, ...]:
    """Parse `extensions-constraints.yaml`: where each extension relation may be used.

    Every entry is validated, but only entries with a `relation_id` are kept. The
    others use relations with no current RO, BFO, or GOREL term, so no annotation
    can match them, and the file's header tells consumers that match by ID to skip
    them. Several entries may name the same relation. A namespace or root term
    listed twice within one entry is kept once.

    Raises:
        InvalidRuleFileError: If the file is invalid.
    """
    constraints: list[ExtensionConstraint] = []
    for entry in _validated(_ExtensionFile, text).root:
        if entry.relation_id is None:
            continue
        constraints.append(
            ExtensionConstraint(
                relation_id=entry.relation_id,
                namespaces=_unique(entry.namespaces),
                primary_root_terms=_unique(entry.primary_root_terms),
                cardinality=entry.cardinality,
            )
        )
    return tuple(constraints)


def _parse_file[T](
    texts: Mapping[str, str],
    path: str,
    parser: Callable[[str], T],
    issues: list[ValidationIssue],
) -> T | None:
    """Parse one file, adding any issues to `issues` under the file's path.

    Returns:
        The parsed value, or `None` if the file is missing or invalid.
    """
    text = texts.get(path)
    if text is None:
        issues.append(
            ValidationIssue(
                location=(path,), message="file is missing", type="missing_file"
            )
        )
        return None
    try:
        return parser(text)
    except InvalidRuleFileError as error:
        issues.extend(
            ValidationIssue(
                location=(path, *issue["location"]),
                message=issue["message"],
                type=issue["type"],
            )
            for issue in error.issues
        )
        return None


def parse_rule_files(texts: Mapping[str, str]) -> RuleFiles:
    """Parse all six files, reporting the problems in every file at once.

    Args:
        texts: The decoded text of each file, keyed by its path in
            `RULE_FILE_PATHS`.

    Raises:
        InvalidRuleFileError: If any file is missing or invalid. Each issue's
            location starts with the file's path.
    """
    issues: list[ValidationIssue] = []
    databases = _parse_file(texts, DB_XREFS_PATH, parse_db_xrefs, issues)
    go_refs = _parse_file(texts, GOREFS_PATH, parse_gorefs, issues)
    groups = _parse_file(texts, GROUPS_PATH, parse_groups, issues)
    retracted = _parse_file(
        texts, RETRACTED_PUBLICATIONS_PATH, parse_retracted_publications, issues
    )
    eco = _parse_file(
        texts, ECO_USAGE_CONSTRAINTS_PATH, parse_eco_usage_constraints, issues
    )
    extensions = _parse_file(
        texts, EXTENSIONS_CONSTRAINTS_PATH, parse_extensions_constraints, issues
    )
    if (
        databases is None
        or go_refs is None
        or groups is None
        or retracted is None
        or eco is None
        or extensions is None
    ):
        raise InvalidRuleFileError(tuple(issues))
    return RuleFiles(
        data=RuleData(
            databases=databases,
            go_refs=go_refs,
            group_shorthands=groups,
            eco_usage_constraints=eco,
            extension_constraints=extensions,
        ),
        retracted_pmids=retracted,
    )
