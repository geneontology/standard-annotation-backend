"""Verify validation and defaults for process configuration."""

import pytest
from pydantic import ValidationError

from standard_annotation_backend.config import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://sab:sab@db:5432/sab",
        "redis_url": "redis://redis:6379/0",
        "application_secret": "test-secret",
        "environment": "testing",
        "log_format": "console",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_authorization_source_defaults_are_safe_go_site_locations() -> None:
    """A default deployment resolves the established go-site users document."""
    settings = _settings()

    assert settings.authorization_source_repository == "geneontology/go-site"
    assert settings.authorization_source_ref == "master"
    assert settings.authorization_source_path == "metadata/users.yaml"
    assert settings.authorization_sync_cron == "0 0 * * *"


@pytest.mark.parametrize("value", ["", "* * *", "60 * * * *"])
def test_authorization_sync_cron_must_be_valid(value: str) -> None:
    """Celery Beat cannot start with a malformed synchronization schedule."""
    with pytest.raises(ValidationError):
        _settings(authorization_sync_cron=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authorization_source_repository", "go-site"),
        ("authorization_source_repository", "owner/repo/extra"),
        ("authorization_source_repository", "owner /repo"),
        ("authorization_source_path", "/metadata/users.yaml"),
        ("authorization_source_path", "../users.yaml"),
        ("authorization_source_path", "metadata/../users.yaml"),
        ("authorization_source_path", "metadata//users.yaml"),
        ("authorization_source_path", "metadata/users.yaml?raw=true"),
    ],
)
def test_authorization_source_rejects_invalid_repository_and_path_shapes(
    field: str,
    value: str,
) -> None:
    """Source locations cannot escape or alter the intended GitHub API path."""
    with pytest.raises(ValidationError):
        _settings(**{field: value})
