"""Configuration loaded from SAB-prefixed environment variables."""

from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

from celery.schedules import crontab
from pydantic import (
    Field,
    PrivateAttr,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from standard_annotation_backend.refresh.sources import (
    RefreshSources,
    SourcesFile,
    load_sources_file,
)
from standard_annotation_backend.validation_types import TrimmedNonBlankString

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

    database_url: TrimmedNonBlankString
    redis_url: TrimmedNonBlankString
    application_secret: TrimmedNonBlankString
    environment: TrimmedNonBlankString
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: SecretStr | None = None
    github_oauth_callback_url: str | None = None
    authorization_refresh_cron: TrimmedNonBlankString = "0 0 * * *"
    ontology_refresh_cron: TrimmedNonBlankString = "0 2 * * 1,3,5"
    entity_refresh_cron: TrimmedNonBlankString = "0 3 * * *"
    annotation_refresh_cron: TrimmedNonBlankString = "0 4 * * *"
    sources_file: Path = Path("config/sources.yaml")
    source_connect_timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    source_read_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    _sources_file: SourcesFile = PrivateAttr()

    @field_validator(
        "authorization_refresh_cron",
        "ontology_refresh_cron",
        "entity_refresh_cron",
        "annotation_refresh_cron",
    )
    @classmethod
    def validate_cron(cls, value: str) -> str:
        """Validate a five-field cron expression that Celery Beat can run.

        Celery's parser checks each field separately, so a date that never
        occurs, such as `0 0 30 2 *` (February 30), parses but has no next run
        time. Celery Beat would raise `RuntimeError` for such a schedule on every
        tick, so it is rejected here, when settings are loaded.
        """
        try:
            crontab.from_string(value).remaining_estimate(datetime.now(UTC))
        except (ValueError, RuntimeError):
            raise ValueError("must be a valid five-field cron schedule") from None
        return value

    @model_validator(mode="after")
    def load_sources(self) -> Self:
        """Read the sources file now, so an invalid file stops process startup."""
        self._sources_file = load_sources_file(self.sources_file)
        return self

    @property
    def sources(self) -> RefreshSources:
        """Return the configured reference data sources."""
        return RefreshSources(self._sources_file)


@lru_cache
def get_logging_settings() -> LoggingSettings:
    """Load and cache settings needed during logging initialization."""
    return LoggingSettings()


@lru_cache
def get_settings() -> Settings:
    """Load and cache the process runtime settings."""
    return Settings()
