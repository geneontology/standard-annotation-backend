"""Configuration loaded from SAB-prefixed environment variables."""

from functools import lru_cache
from typing import Annotated

from pydantic import StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Settings(BaseSettings):
    """Runtime settings shared by the API and worker processes."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SAB_",
        extra="ignore",
    )

    database_url: NonEmptyString
    redis_url: NonEmptyString
    application_secret: NonEmptyString
    environment: NonEmptyString


@lru_cache
def get_settings() -> Settings:
    """Load and cache the process runtime settings."""
    return Settings()
