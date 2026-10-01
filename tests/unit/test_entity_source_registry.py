"""Verify the entity source registry file, settings, and lookups."""

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from standard_annotation_backend.config import Settings
from standard_annotation_backend.entity_sources.registry import (
    ConfiguredEntitySource,
    EntitySourceRegistry,
)

COMMITTED_REGISTRY = Path(__file__).parents[2] / "config" / "entity-sources.yaml"


def _settings(registry: Path, **overrides: object) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://sab:sab@db:5432/sab",
        "redis_url": "redis://redis:6379/0",
        "application_secret": "test-secret",
        "environment": "testing",
        "log_format": "console",
        "entity_sources_file": registry,
    }
    values.update(overrides)
    with patch.dict(os.environ, {}, clear=True):
        return Settings(_env_file=None, **values)


def _registry_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "entity-sources.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_registry_file_loads_sources_with_default_format(tmp_path: Path) -> None:
    """Sources are listed in sorted key order, and the format defaults to `gpi`."""
    settings = _settings(
        _registry_file(
            tmp_path,
            "sources:\n"
            "  rgd:\n    url: https://example.org/rgd.gpi.gz\n"
            "  mgi:\n    format: gpi\n    url: https://example.org/mgi.gpi.gz\n",
        )
    )
    registry = EntitySourceRegistry(settings.entity_sources)

    assert registry.keys == ("mgi", "rgd")
    assert registry.source("rgd") == ConfiguredEntitySource(
        key="rgd", format="gpi", url="https://example.org/rgd.gpi.gz"
    )


@pytest.mark.parametrize(
    "text", ["sources: {}\n", "sources:\n", "", "# only a comment\n"]
)
def test_empty_registry_is_valid(tmp_path: Path, text: str) -> None:
    """A registry with no sources is allowed and has no keys."""
    settings = _settings(_registry_file(tmp_path, text))
    assert EntitySourceRegistry(settings.entity_sources).keys == ()


@pytest.mark.parametrize(
    "text",
    [
        "sources:\n  MGI:\n    url: https://example.org/mgi.gpi\n",
        "sources:\n  -mgi:\n    url: https://example.org/mgi.gpi\n",
        "sources:\n  'm gi':\n    url: https://example.org/mgi.gpi\n",
        "sources:\n  mgi:\n    url: http://example.org/mgi.gpi\n",
        "sources:\n  mgi:\n    url: not-a-url\n",
        "sources:\n  mgi:\n    format: gaf\n    url: https://example.org/mgi.gaf\n",
        "sources:\n  mgi:\n    url: https://example.org/mgi.gpi\n    sha256: abc\n",
        "sources:\n  mgi: {}\n",
        "sources:\n  - mgi\n",
        "sources: https://example.org/mgi.gpi\n",
        "entities:\n  mgi:\n    url: https://example.org/mgi.gpi\n",
        "sources: [\n",
    ],
)
def test_invalid_registry_fails_settings_construction(
    tmp_path: Path, text: str
) -> None:
    """An invalid registry stops startup instead of failing later inside a job."""
    with pytest.raises(ValidationError):
        _settings(_registry_file(tmp_path, text))


def test_missing_registry_file_fails_settings_construction(tmp_path: Path) -> None:
    """A missing registry file stops startup."""
    with pytest.raises(ValidationError):
        _settings(tmp_path / "missing.yaml")


def test_committed_registry_file_is_valid() -> None:
    """The registry shipped in the repository loads successfully."""
    settings = _settings(COMMITTED_REGISTRY)
    assert isinstance(EntitySourceRegistry(settings.entity_sources).keys, tuple)


def test_entity_import_settings_defaults(tmp_path: Path) -> None:
    """The import schedule defaults to 03:00 daily; timeouts to 10 and 60 seconds."""
    settings = _settings(_registry_file(tmp_path, "sources: {}\n"))
    assert settings.entity_import_cron == "0 3 * * *"
    assert settings.entity_source_connect_timeout_seconds == 10
    assert settings.entity_source_read_timeout_seconds == 60


@pytest.mark.parametrize("value", ["", "not cron", "61 * * * *"])
def test_entity_import_cron_must_be_valid(tmp_path: Path, value: str) -> None:
    """Invalid cron expressions for the entity import schedule are rejected."""
    with pytest.raises(ValidationError):
        _settings(_registry_file(tmp_path, "sources: {}\n"), entity_import_cron=value)


@pytest.mark.parametrize(
    "field",
    ["entity_source_connect_timeout_seconds", "entity_source_read_timeout_seconds"],
)
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_entity_source_timeouts_must_be_positive_and_finite(
    tmp_path: Path, field: str, value: float
) -> None:
    """Zero, negative, infinite, and NaN source timeouts are rejected."""
    with pytest.raises(ValidationError):
        _settings(_registry_file(tmp_path, "sources: {}\n"), **{field: value})
