"""Configuration loaded from SAB-prefixed environment variables."""

import string
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from celery.schedules import crontab
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    PrivateAttr,
    SecretStr,
    StringConstraints,
    Tag,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from standard_annotation_backend.domain.ontology import OntologyKey
from standard_annotation_backend.validation_types import TrimmedNonBlankString

SETTINGS_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    env_prefix="SAB_",
    env_nested_delimiter="__",
    extra="ignore",
)
_GITHUB_PATH_SEGMENT_CHARACTERS = frozenset(
    string.ascii_letters + string.digits + "._-"
)


def _is_github_path_segment(value: str) -> bool:
    """Check that a path segment is nonempty and uses allowed characters.

    Allowed characters are ASCII letters, digits, `.`, `_`, and `-`.
    """
    return bool(value) and set(value) <= _GITHUB_PATH_SEGMENT_CHARACTERS


class LogFormat(StrEnum):
    """Supported process log renderers."""

    CONSOLE = "console"
    JSON = "json"


class LoggingSettings(BaseSettings):
    """Settings required before application startup can log."""

    model_config = SETTINGS_CONFIG

    log_format: LogFormat


class GitHubOntologySourceSettings(BaseModel):
    """Configure one ontology document stored in a GitHub repository."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: Literal["github"] = "github"
    repository: str
    ref: TrimmedNonBlankString
    path: str

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        """Validate a GitHub repository in `owner/repository` form."""
        return _validated_github_repository(value)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Validate a relative path within a GitHub repository."""
        return _validated_github_path(value)


OntologySourceSettings = Annotated[
    GitHubOntologySourceSettings,
    Field(discriminator="source_type"),
]


SOURCE_KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"

SourceKey = Annotated[str, StringConstraints(pattern=SOURCE_KEY_PATTERN)]


class GpiEntitySourceSettings(BaseModel):
    """Configure one entity source published as a GPI 2.0 file over HTTPS."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["gpi"] = "gpi"
    url: AnyHttpUrl

    @field_validator("url")
    @classmethod
    def require_https(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Require HTTPS for every configured source URL."""
        if value.scheme != "https":
            raise ValueError("must use https")
        return value


def _entity_source_format(value: object) -> str | None:
    """Return an entry's `format` value, or `gpi` when it is omitted.

    Pydantic calls this to choose which settings model validates the entry.
    """
    if isinstance(value, Mapping):
        return str(value.get("format", "gpi"))
    return str(getattr(value, "format", "gpi"))


EntitySourceSettings = Annotated[
    Annotated[GpiEntitySourceSettings, Tag("gpi")],
    Discriminator(_entity_source_format),
]


class EntitySourcesFile(BaseModel):
    """Describe the contents of the entity source registry file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: dict[SourceKey, EntitySourceSettings] = Field(default_factory=dict)

    @field_validator("sources", mode="before")
    @classmethod
    def _null_sources_are_empty(cls, value: object) -> object:
        """Treat a `sources:` key with no value as an empty registry."""
        return {} if value is None else value


def load_entity_sources(path: Path) -> EntitySourcesFile:
    """Read and validate the entity source registry file.

    An empty file, or one containing only comments, is an empty registry.

    Args:
        path: Location of the YAML registry file.

    Returns:
        The validated registry contents.

    Raises:
        ValueError: If the file cannot be read, is not valid YAML, or does not
            match the registry schema.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise ValueError("entity source registry file could not be read") from None
    return EntitySourcesFile.model_validate({} if raw is None else raw)


def _default_ontology_sources() -> dict[OntologyKey, OntologySourceSettings]:
    """Return the default GitHub source configuration for GO."""
    return {
        OntologyKey.GO: GitHubOntologySourceSettings(
            repository="geneontology/go-ontology",
            ref="master",
            path="src/ontology/go-edit.obo",
        )
    }


class Settings(LoggingSettings):
    """Runtime settings shared by the API and worker processes."""

    database_url: TrimmedNonBlankString
    redis_url: TrimmedNonBlankString
    application_secret: TrimmedNonBlankString
    environment: TrimmedNonBlankString
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: SecretStr | None = None
    github_oauth_callback_url: str | None = None
    authorization_source_repository: str = "geneontology/go-site"
    authorization_source_ref: TrimmedNonBlankString = "master"
    authorization_source_path: str = "metadata/users.yaml"
    authorization_sync_cron: TrimmedNonBlankString = "0 0 * * *"
    ontology_sources: dict[OntologyKey, OntologySourceSettings] = Field(
        default_factory=_default_ontology_sources
    )
    ontology_load_cron: TrimmedNonBlankString = "0 2 * * 1,3,5"
    entity_sources_file: Path = Path("config/entity-sources.yaml")
    entity_import_cron: TrimmedNonBlankString = "0 3 * * *"
    entity_source_connect_timeout_seconds: float = Field(
        default=10, gt=0, allow_inf_nan=False
    )
    entity_source_read_timeout_seconds: float = Field(
        default=60, gt=0, allow_inf_nan=False
    )
    _entity_sources: EntitySourcesFile = PrivateAttr()

    @field_validator(
        "authorization_sync_cron", "ontology_load_cron", "entity_import_cron"
    )
    @classmethod
    def validate_cron(cls, value: str) -> str:
        """Validate a five-field cron expression for Celery Beat."""
        try:
            crontab.from_string(value)
        except ValueError:
            raise ValueError("must be a valid five-field cron schedule") from None
        return value

    @model_validator(mode="after")
    def load_entity_source_registry(self) -> Self:
        """Read the registry file now, so an invalid file stops process startup."""
        self._entity_sources = load_entity_sources(self.entity_sources_file)
        return self

    @property
    def entity_sources(self) -> Mapping[str, EntitySourceSettings]:
        """Return configured entity sources keyed by source key."""
        return self._entity_sources.sources

    @field_validator("ontology_sources")
    @classmethod
    def validate_ontology_sources(
        cls,
        value: dict[OntologyKey, OntologySourceSettings],
    ) -> dict[OntologyKey, OntologySourceSettings]:
        """Require configuration for every ontology accepted by the API."""
        if OntologyKey.GO not in value:
            raise ValueError("must configure the go ontology")
        return value

    @field_validator("authorization_source_repository")
    @classmethod
    def validate_authorization_source_repository(cls, value: str) -> str:
        """Validate a GitHub repository in `owner/repository` form."""
        return _validated_github_repository(value)

    @field_validator("authorization_source_path")
    @classmethod
    def validate_authorization_source_path(cls, value: str) -> str:
        """Validate a relative GitHub path with no empty or traversal segments."""
        return _validated_github_path(value)


def _validated_github_repository(value: str) -> str:
    """Return a normalized `owner/repository` name or raise `ValueError`."""
    normalized = value.strip()
    parts = normalized.split("/")
    if len(parts) != 2 or not all(_is_github_path_segment(part) for part in parts):
        raise ValueError("must be a GitHub owner/repository name")
    return normalized


def _validated_github_path(value: str) -> str:
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


@lru_cache
def get_logging_settings() -> LoggingSettings:
    """Load and cache settings needed during logging initialization."""
    return LoggingSettings()


@lru_cache
def get_settings() -> Settings:
    """Load and cache the process runtime settings."""
    return Settings()
