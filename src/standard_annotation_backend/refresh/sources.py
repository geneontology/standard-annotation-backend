"""Read the reviewed file that configures every reference data source.

`config/sources.yaml` lists the authorization source, the go-site repository
whose metadata files GO Rules read, each ontology source, each entity source, and
each group's GPAD annotation source. Most entries are a file in a GitHub
repository or an HTTPS URL. The `rules` entry names only a GitHub repository and a
ref (a branch, tag, or commit), because SABE reads a fixed set of files from it.
`Settings` validates the file when it is constructed, so an invalid file stops the
API, worker, scheduler, and CLI at startup instead of failing later inside a job.
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, ClassVar, Literal

import yaml
from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from standard_annotation_backend.domain.ontology import (
    GO_ONTOLOGY_KEY,
    REQUIRED_GO_SUBSETS,
    SUBCLASS_PREDICATE,
    OntologyCatalog,
    OntologyDefinition,
)
from standard_annotation_backend.domain.refresh import (
    AUTHORIZATION_SOURCE_KEY,
    RULES_SOURCE_KEY,
    SOURCE_KEY_PATTERN,
    RefreshKindName,
    UnknownSourceError,
)
from standard_annotation_backend.go_site.rule_files import RULE_FILE_PATHS
from standard_annotation_backend.validation_types import TrimmedNonBlankString
from standard_annotation_backend.yaml_loading import load_yaml

SourceKey = Annotated[str, StringConstraints(pattern=SOURCE_KEY_PATTERN)]

OntologyPrefix = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")]
"""An identifier prefix that an ontology owns, such as `GO` or `NCBITaxon`."""


def _without_repeats(values: tuple[str, ...]) -> tuple[str, ...]:
    """Reject a list that names the same value more than once."""
    if len(set(values)) != len(values):
        raise ValueError("must not list the same value twice")
    return values


OntologyPrefixes = Annotated[
    tuple[OntologyPrefix, ...], Field(min_length=1), AfterValidator(_without_repeats)
]
"""The identifier prefixes an ontology owns; at least one, none repeated."""

ClosurePredicates = Annotated[
    tuple[TrimmedNonBlankString, ...], AfterValidator(_without_repeats)
]
"""The relations SABE follows to compute each term's ancestors; none repeated."""

RequiredSubsets = Annotated[
    tuple[TrimmedNonBlankString, ...], AfterValidator(_without_repeats)
]
"""The subsets an ontology's file must declare; none repeated."""

_GITHUB_PATH_SEGMENT_CHARACTERS = frozenset(
    string.ascii_letters + string.digits + "._-"
)


class GitHubSource(BaseModel):
    """Configure a file stored in a GitHub repository.

    Each fetch resolves `ref` to a commit and downloads `path` at that commit, so
    the stored provenance always names an immutable revision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["github"]
    repository: str
    ref: TrimmedNonBlankString
    path: str

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        """Validate a GitHub repository in `owner/repository` form."""
        return validated_github_repository(value)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Validate a relative path within a GitHub repository."""
        return validated_github_path(value)


class HttpsSource(BaseModel):
    """Configure a file published at an HTTPS URL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["https"]
    url: AnyHttpUrl

    @field_validator("url")
    @classmethod
    def require_https(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Require HTTPS for every configured URL."""
        if value.scheme != "https":
            raise ValueError("must use https")
        return value


class GitHubEntitySource(GitHubSource):
    """Configure an entity source stored in GitHub.

    Attributes:
        format: Serialization format of the file. GPI 2.0 is the only format.
    """

    format: Literal["gpi"] = "gpi"


class HttpsEntitySource(HttpsSource):
    """Configure an entity source published over HTTPS.

    Attributes:
        format: Serialization format of the file. GPI 2.0 is the only format.
    """

    format: Literal["gpi"] = "gpi"


class GitHubAnnotationSource(GitHubSource):
    """Configure a group's GPAD annotation source stored in GitHub.

    Attributes:
        group: Group that owns every annotation in the file. Each successful
            refresh replaces all of this group's annotations.
        format: Serialization format of the file. GPAD 2.0 is the only format.
    """

    group: TrimmedNonBlankString
    format: Literal["gpad"] = "gpad"


class HttpsAnnotationSource(HttpsSource):
    """Configure a group's GPAD annotation source published over HTTPS.

    Attributes:
        group: Group that owns every annotation in the file. Each successful
            refresh replaces all of this group's annotations.
        format: Serialization format of the file. GPAD 2.0 is the only format.
    """

    group: TrimmedNonBlankString
    format: Literal["gpad"] = "gpad"


class GitHubRulesSource(BaseModel):
    """Configure the go-site repository whose metadata files GO Rules read.

    Each fetch resolves `ref` to one commit and downloads every file in `paths` at
    that commit, so the files always come from the same revision.

    Attributes:
        paths: The files read from the repository. They cannot be configured,
            because SABE has a parser for each one.
        repository: The repository in `owner/repository` form, such as
            `geneontology/go-site`.
        ref: The branch, tag, or commit to read, such as `master`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    paths: ClassVar[tuple[str, ...]] = RULE_FILE_PATHS

    type: Literal["github"]
    repository: str
    ref: TrimmedNonBlankString

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        """Validate a GitHub repository in `owner/repository` form."""
        return validated_github_repository(value)


class GitHubOntologySource(GitHubSource):
    """Configure an OBO ontology stored in GitHub.

    Attributes:
        document_ontology_id: The `ontology:` header value the file must declare,
            such as `go` or `cl/cl-basic`.
        prefixes: Identifier prefixes the ontology owns, such as `GO`.
        closure_predicates: Relations SABE follows to compute each term's
            ancestors, such as `rdfs:subClassOf`. Omit them to load the terms
            without computing ancestors.
        required_subsets: Subsets the file must declare, such as
            `gocheck_do_not_annotate`. A refresh rejects a file that lacks one.
    """

    document_ontology_id: TrimmedNonBlankString
    prefixes: OntologyPrefixes
    closure_predicates: ClosurePredicates = ()
    required_subsets: RequiredSubsets = ()


class HttpsOntologySource(HttpsSource):
    """Configure an OBO ontology published over HTTPS.

    Attributes:
        document_ontology_id: The `ontology:` header value the file must declare,
            such as `go` or `cl/cl-basic`.
        prefixes: Identifier prefixes the ontology owns, such as `GO`.
        closure_predicates: Relations SABE follows to compute each term's
            ancestors, such as `rdfs:subClassOf`. Omit them to load the terms
            without computing ancestors.
        required_subsets: Subsets the file must declare, such as
            `gocheck_do_not_annotate`. A refresh rejects a file that lacks one.
    """

    document_ontology_id: TrimmedNonBlankString
    prefixes: OntologyPrefixes
    closure_predicates: ClosurePredicates = ()
    required_subsets: RequiredSubsets = ()


SourceSpec = Annotated[GitHubSource | HttpsSource, Field(discriminator="type")]
OntologySourceSpec = Annotated[
    GitHubOntologySource | HttpsOntologySource, Field(discriminator="type")
]
EntitySourceSpec = Annotated[
    GitHubEntitySource | HttpsEntitySource, Field(discriminator="type")
]
AnnotationSourceSpec = Annotated[
    GitHubAnnotationSource | HttpsAnnotationSource, Field(discriminator="type")
]
ConfiguredSource = GitHubSource | HttpsSource | GitHubRulesSource
"""Any one configured source. `SourceFetchers` has a fetcher for each type."""


class SourcesFile(BaseModel):
    """Describe the contents of `config/sources.yaml`.

    Attributes:
        authorization: The single go-site `users.yaml` source.
        rules: The go-site repository whose metadata files GO Rules read. It must
            be on GitHub, so that every file comes from one commit.
        ontologies: Ontology sources keyed by ontology, each saying how to read its
            file. GO is required.
        entities: Entity sources keyed by stable source key. Keys are stored with
            published catalogs, so renaming a key retires the old catalog.
        annotations: GPAD annotation sources keyed by stable source key. No two
            may name the same group, because each refresh replaces the whole group.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization: SourceSpec
    rules: GitHubRulesSource
    ontologies: dict[SourceKey, OntologySourceSpec]
    entities: dict[SourceKey, EntitySourceSpec] = Field(default_factory=dict)
    annotations: dict[SourceKey, AnnotationSourceSpec] = Field(default_factory=dict)

    @field_validator("ontologies", "entities", "annotations", mode="before")
    @classmethod
    def null_sections_are_empty(cls, value: object) -> object:
        """Treat a section key with no value as an empty mapping."""
        return {} if value is None else value

    @field_validator("ontologies")
    @classmethod
    def require_go_with_what_go_rules_use(
        cls, value: dict[str, GitHubOntologySource | HttpsOntologySource]
    ) -> dict[str, GitHubOntologySource | HttpsOntologySource]:
        """Require a GO entry with what GO Rules and descendant searches need.

        The entry must list `rdfs:subClassOf` in `closure_predicates` and every
        subset GO Rules check in `required_subsets`. Without them, GO Rules and
        searches that include a term's descendants would silently stop working.
        """
        go = value.get(GO_ONTOLOGY_KEY)
        if go is None:
            raise ValueError("must configure the GO ontology")
        if SUBCLASS_PREDICATE not in go.closure_predicates:
            raise ValueError(
                f"the GO ontology must list {SUBCLASS_PREDICATE} in closure_predicates"
            )
        missing = [
            subset
            for subset in REQUIRED_GO_SUBSETS
            if subset not in go.required_subsets
        ]
        if missing:
            raise ValueError(
                "the GO ontology must list these in required_subsets: "
                + ", ".join(missing)
            )
        return value

    @field_validator("ontologies")
    @classmethod
    def require_one_ontology_per_prefix(
        cls, value: dict[str, GitHubOntologySource | HttpsOntologySource]
    ) -> dict[str, GitHubOntologySource | HttpsOntologySource]:
        """Reject a prefix two ontologies share, so each identifier has one ontology."""
        owners: dict[str, list[str]] = {}
        for key, source in value.items():
            for prefix in source.prefixes:
                owners.setdefault(prefix, []).append(key)
        shared = sorted(prefix for prefix, keys in owners.items() if len(keys) > 1)
        if shared:
            raise ValueError(
                "each identifier prefix may belong to only one ontology: "
                + ", ".join(shared)
            )
        return value

    @field_validator("annotations")
    @classmethod
    def require_one_source_per_group(
        cls, value: dict[str, GitHubAnnotationSource | HttpsAnnotationSource]
    ) -> dict[str, GitHubAnnotationSource | HttpsAnnotationSource]:
        """Reject two sources for one group; each would replace the other's data."""
        groups = [source.group for source in value.values()]
        shared = sorted({group for group in groups if groups.count(group) > 1})
        if shared:
            raise ValueError(
                "each group may have only one annotation source: " + ", ".join(shared)
            )
        return value


def load_sources_file(path: Path) -> SourcesFile:
    """Read and validate the sources file.

    Args:
        path: Location of the YAML file.

    Returns:
        The validated file contents.

    Raises:
        ValueError: If the file cannot be read, is not valid YAML, repeats a key
            within one mapping, or does not match `SourcesFile`. Pydantic's
            `ValidationError` is a `ValueError`.
    """
    try:
        raw = load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise ValueError("sources file could not be read") from None
    return SourcesFile.model_validate({} if raw is None else raw)


class RefreshSources:
    """Look up configured sources by refresh kind and source key."""

    def __init__(self, file: SourcesFile) -> None:
        self._sources: dict[RefreshKindName, dict[str, ConfiguredSource]] = {
            RefreshKindName.AUTHORIZATION: {
                AUTHORIZATION_SOURCE_KEY: file.authorization
            },
            RefreshKindName.RULES: {RULES_SOURCE_KEY: file.rules},
            RefreshKindName.ONTOLOGY: dict(file.ontologies),
            RefreshKindName.ENTITY: dict(file.entities),
            RefreshKindName.ANNOTATION: dict(file.annotations),
        }
        self._annotation_groups = {
            key: source.group for key, source in file.annotations.items()
        }
        self._ontology_definitions = {
            key: OntologyDefinition(
                key=key,
                identifier_prefixes=source.prefixes,
                closure_predicates=source.closure_predicates,
                document_ontology_id=source.document_ontology_id,
                required_subsets=source.required_subsets,
            )
            for key, source in file.ontologies.items()
        }

    def sources(self, kind: RefreshKindName) -> Mapping[str, ConfiguredSource]:
        """Return a copy of one kind's sources keyed by source key."""
        return dict(self._sources[kind])

    def keys(self, kind: RefreshKindName) -> tuple[str, ...]:
        """Return one kind's configured source keys in sorted order."""
        return tuple(sorted(self._sources[kind]))

    def source(self, kind: RefreshKindName, key: str) -> ConfiguredSource:
        """Return one configured source.

        Raises:
            UnknownSourceError: If `key` is not configured for `kind`.
        """
        source = self._sources[kind].get(key)
        if source is None:
            raise UnknownSourceError(kind, key)
        return source

    def annotation_group(self, key: str) -> str:
        """Return the group that owns the annotations of one annotation source.

        Raises:
            UnknownSourceError: If `key` is not a configured annotation source.
        """
        group = self._annotation_groups.get(key)
        if group is None:
            raise UnknownSourceError(RefreshKindName.ANNOTATION, key)
        return group

    def ontology_definition(self, key: str) -> OntologyDefinition:
        """Return how to read one configured ontology's file.

        Raises:
            UnknownSourceError: If `key` is not a configured ontology.
        """
        definition = self._ontology_definitions.get(key)
        if definition is None:
            raise UnknownSourceError(RefreshKindName.ONTOLOGY, key)
        return definition

    def ontology_catalog(self) -> OntologyCatalog:
        """Return the configured ontologies and the identifier prefixes each owns."""
        return OntologyCatalog(
            {
                key: definition.identifier_prefixes
                for key, definition in self._ontology_definitions.items()
            }
        )


def _is_github_path_segment(value: str) -> bool:
    """Check that a path segment is nonempty and uses allowed characters.

    Allowed characters are ASCII letters, digits, `.`, `_`, and `-`.
    """
    return bool(value) and set(value) <= _GITHUB_PATH_SEGMENT_CHARACTERS


def validated_github_repository(value: str) -> str:
    """Return a normalized `owner/repository` name or raise `ValueError`."""
    normalized = value.strip()
    parts = normalized.split("/")
    if len(parts) != 2 or not all(_is_github_path_segment(part) for part in parts):
        raise ValueError("must be a GitHub owner/repository name")
    return normalized


def validated_github_path(value: str) -> str:
    """Return a relative GitHub path with no empty or traversal segments."""
    normalized = value.strip()
    parts = normalized.split("/")
    if (
        normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or not all(_is_github_path_segment(part) for part in parts)
    ):
        raise ValueError("must be a safe relative GitHub content path")
    return normalized
