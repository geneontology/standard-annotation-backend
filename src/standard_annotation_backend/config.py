"""Configuration loaded from SAB-prefixed environment variables."""

import string
from enum import StrEnum
from functools import lru_cache

from celery.schedules import crontab
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from standard_annotation_backend.validation_types import TrimmedNonBlankString

SETTINGS_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    env_prefix="SAB_",
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

    @field_validator("authorization_sync_cron")
    @classmethod
    def validate_authorization_sync_cron(cls, value: str) -> str:
        """Validate a five-field cron expression for Celery Beat."""
        try:
            crontab.from_string(value)
        except ValueError:
            raise ValueError("must be a valid five-field cron schedule") from None
        return value

    @field_validator("authorization_source_repository")
    @classmethod
    def validate_authorization_source_repository(cls, value: str) -> str:
        """Validate a GitHub repository in `owner/repository` form."""
        normalized = value.strip()
        parts = normalized.split("/")
        if len(parts) != 2 or not all(_is_github_path_segment(part) for part in parts):
            raise ValueError("must be a GitHub owner/repository name")
        return normalized

    @field_validator("authorization_source_path")
    @classmethod
    def validate_authorization_source_path(cls, value: str) -> str:
        """Validate a relative GitHub path with no empty or traversal segments."""
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
