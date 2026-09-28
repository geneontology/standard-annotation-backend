"""Verify validation and defaults for process configuration."""

import os
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from standard_annotation_backend.config import Settings
from standard_annotation_backend.domain.ontology import OntologyKey


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


def test_authorization_source_defaults_are_safe_go_site_locations() -> None:
    """A default deployment resolves the established go-site users document."""
    settings = _settings()

    assert settings.authorization_source_repository == "geneontology/go-site"
    assert settings.authorization_source_ref == "master"
    assert settings.authorization_source_path == "metadata/users.yaml"
    assert settings.authorization_sync_cron == "0 0 * * *"


def test_ontology_source_defaults_allowlist_go_and_a_three_day_schedule() -> None:
    """Default ontology loading targets the approved GO source on separate days."""
    settings = _settings()

    assert set(settings.ontology_sources) == {OntologyKey.GO}
    source = settings.ontology_sources[OntologyKey.GO]
    assert source.source_type == "github"
    assert source.repository == "geneontology/go-ontology"
    assert source.ref == "master"
    assert source.path == "src/ontology/go-edit.obo"
    assert settings.ontology_load_cron == "0 2 * * 1,3,5"


def test_nested_environment_configures_the_go_ontology_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators can configure one ontology without encoding the registry as JSON."""
    monkeypatch.setenv("SAB_DATABASE_URL", "postgresql://database/sab")
    monkeypatch.setenv("SAB_REDIS_URL", "redis://redis/0")
    monkeypatch.setenv("SAB_APPLICATION_SECRET", "test-secret")
    monkeypatch.setenv("SAB_ENVIRONMENT", "testing")
    monkeypatch.setenv("SAB_LOG_FORMAT", "console")
    monkeypatch.setenv("SAB_ONTOLOGY_SOURCES__GO__SOURCE_TYPE", "github")
    monkeypatch.setenv("SAB_ONTOLOGY_SOURCES__GO__REPOSITORY", "example/ontology")
    monkeypatch.setenv("SAB_ONTOLOGY_SOURCES__GO__REF", "release")
    monkeypatch.setenv("SAB_ONTOLOGY_SOURCES__GO__PATH", "ontology/go.obo")

    settings = Settings(_env_file=None)

    source = settings.ontology_sources[OntologyKey.GO]
    assert source.source_type == "github"
    assert source.repository == "example/ontology"
    assert source.ref == "release"
    assert source.path == "ontology/go.obo"


@pytest.mark.parametrize("value", ["", "* * *", "60 * * * *"])
def test_authorization_sync_cron_must_be_valid(value: str) -> None:
    """Celery Beat cannot start with a malformed synchronization schedule."""
    with pytest.raises(ValidationError):
        _settings(authorization_sync_cron=value)


@pytest.mark.parametrize("value", ["", "* * *", "60 * * * *"])
def test_ontology_load_cron_must_be_valid(value: str) -> None:
    """Celery Beat cannot start with a malformed ontology schedule."""
    with pytest.raises(ValidationError):
        _settings(ontology_load_cron=value)


def test_ontology_sources_require_go_configuration() -> None:
    """Startup rejects a registry that cannot serve the public GO key."""
    with pytest.raises(ValidationError, match="must configure the go ontology"):
        _settings(ontology_sources={})


@pytest.mark.parametrize(
    "source",
    [
        {"source_type": "url", "url": "https://example.org/go.obo"},
        {
            "source_type": "github",
            "repository": "geneontology/go-ontology",
            "ref": "master",
            "path": "../go-edit.obo",
        },
        {
            "source_type": "github",
            "repository": "geneontology/go-ontology",
            "ref": "master",
            "path": "src/ontology/go-edit.obo",
            "unexpected": True,
        },
    ],
)
def test_ontology_sources_reject_unimplemented_or_unsafe_configuration(
    source: dict[str, object],
) -> None:
    """SAB rejects incomplete or unsupported GitHub source configuration."""
    with pytest.raises(ValidationError):
        _settings(ontology_sources={"go": source})


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
