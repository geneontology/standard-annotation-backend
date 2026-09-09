"""Tests for loading schema artifacts from the installed dependency."""

from standard_annotation_backend.domain.schema_artifacts import (
    SCHEMA_ID,
    load_json_schema,
)


def test_json_schema_is_loaded_from_installed_package() -> None:
    json_schema = load_json_schema()

    assert json_schema["$id"] == SCHEMA_ID
    definitions = json_schema["$defs"]
    assert isinstance(definitions, dict)
    assert "Annotation" in definitions


def test_application_startup_exposes_packaged_json_schema(client) -> None:
    json_schema = client.app.state.standard_annotation_json_schema

    assert json_schema["$id"] == SCHEMA_ID
