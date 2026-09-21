"""Configuration loaded from SAB-prefixed environment variables."""

from enum import StrEnum
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from standard_annotation_backend.validation_types import NonBlankString

SETTINGS_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    env_prefix="SAB_",
    extra="ignore",
)


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

    database_url: NonBlankString
    redis_url: NonBlankString
    application_secret: NonBlankString
    environment: NonBlankString
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: SecretStr | None = None
    github_oauth_callback_url: str | None = None


@lru_cache
def get_logging_settings() -> LoggingSettings:
    """Load and cache settings needed during logging initialization."""
    return LoggingSettings()


@lru_cache
def get_settings() -> Settings:
    """Load and cache the process runtime settings."""
    return Settings()
