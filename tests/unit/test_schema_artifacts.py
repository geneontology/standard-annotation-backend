"""Tests for loading schema artifacts from the installed dependency."""

from importlib.metadata import version

from standard_annotation_backend.domain.schema_artifacts import (
    SCHEMA_ID,
    load_schema_artifacts,
)


def test_schema_artifacts_are_loaded_from_installed_package() -> None:
    artifacts = load_schema_artifacts()

    assert artifacts.package_version == version("go-standard-annotation-schema")
    assert artifacts.json_schema["$id"] == SCHEMA_ID
    definitions = artifacts.json_schema["$defs"]
    assert isinstance(definitions, dict)
    assert "Annotation" in definitions


def test_application_startup_exposes_packaged_schema_artifacts(client) -> None:
    artifacts = client.app.state.schema_artifacts

    assert artifacts.package_version == version("go-standard-annotation-schema")
    assert artifacts.json_schema["$id"] == SCHEMA_ID
