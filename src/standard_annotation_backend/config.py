"""Configuration loaded from SAB-prefixed environment variables."""

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

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

    database_url: NonEmptyString
    redis_url: NonEmptyString
    application_secret: NonEmptyString
    environment: NonEmptyString


@lru_cache
def get_logging_settings() -> LoggingSettings:
    """Load and cache settings needed during logging initialization."""
    return LoggingSettings()


@lru_cache
def get_settings() -> Settings:
    """Load and cache the process runtime settings."""
    return Settings()
