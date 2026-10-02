"""Verify validation and defaults for process configuration."""

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from standard_annotation_backend.config import Settings
from standard_annotation_backend.domain.refresh import RefreshKindName


def _settings(**overrides: object) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://sab:sab@db:5432/sab",
        "redis_url": "redis://redis:6379/0",
        "application_secret": "test-secret",
        "environment": "testing",
        "log_format": "console",
    }
    values.update(overrides)
    with patch.dict(os.environ, {}, clear=True):
        return Settings(_env_file=None, **values)


def test_refresh_schedules_default_to_the_documented_times() -> None:
    """Authorization refreshes daily at 00:00, ontologies Mon/Wed/Fri 02:00, entities 03:00."""
    settings = _settings()

    assert settings.authorization_refresh_cron == "0 0 * * *"
    assert settings.ontology_refresh_cron == "0 2 * * 1,3,5"
    assert settings.entity_refresh_cron == "0 3 * * *"


@pytest.mark.parametrize(
    "setting",
    ["authorization_refresh_cron", "ontology_refresh_cron", "entity_refresh_cron"],
)
@pytest.mark.parametrize("value", ["", "* * *", "60 * * * *"])
def test_refresh_schedules_must_be_valid_cron(setting: str, value: str) -> None:
    """Celery Beat cannot start with a malformed refresh schedule."""
    with pytest.raises(ValidationError):
        _settings(**{setting: value})


def test_settings_expose_the_shipped_sources_file() -> None:
    """The default sources file is loaded and exposed through `sources`."""
    settings = _settings()

    assert settings.sources_file == Path("config/sources.yaml")
    assert settings.sources.keys(RefreshKindName.ONTOLOGY) == ("go",)
    assert settings.source_connect_timeout_seconds == 10
    assert settings.source_read_timeout_seconds == 60


@pytest.mark.parametrize(
    "text",
    [
        "authorization: {type: ftp, url: 'ftp://example.org/u.yaml'}\n",
        "authorization: {type: https, url: 'https://e.org/u'}\nontologies: {}\n",
        "",
    ],
)
def test_invalid_sources_file_stops_settings_construction(
    tmp_path: Path, text: str
) -> None:
    """An invalid sources file stops every process at startup."""
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValidationError):
        _settings(sources_file=path)


def test_missing_sources_file_stops_settings_construction(tmp_path: Path) -> None:
    """A missing sources file stops startup."""
    with pytest.raises(ValidationError):
        _settings(sources_file=tmp_path / "missing.yaml")


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_source_timeouts_must_be_positive_and_finite(value: float) -> None:
    """Timeouts must be usable by the HTTP client."""
    with pytest.raises(ValidationError):
        _settings(source_read_timeout_seconds=value)
