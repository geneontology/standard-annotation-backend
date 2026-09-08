"""Configuration loaded from SAB-prefixed environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by the API and worker processes."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SAB_",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://sab:sab@localhost:5432/sab"
    redis_url: str = "redis://localhost:6379/0"
    application_secret: str = "local-development-secret"
    environment: str = "development"


settings = Settings()
