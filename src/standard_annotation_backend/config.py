"""Configuration loaded from SAB-prefixed environment variables."""

import string
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from celery.schedules import crontab
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
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

    @field_validator("authorization_sync_cron", "ontology_load_cron")
    @classmethod
    def validate_cron(cls, value: str) -> str:
        """Validate a five-field cron expression for Celery Beat."""
        try:
            crontab.from_string(value)
        except ValueError:
            raise ValueError("must be a valid five-field cron schedule") from None
        return value

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
