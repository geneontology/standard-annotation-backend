"""Load schema artifacts bundled with the Standard Annotation dependency."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.metadata import version
from importlib.resources import files

SCHEMA_ID = "https://w3id.org/geneontology/go-standard-annotation-schema"
_PACKAGE_NAME = "go-standard-annotation-schema"
_PACKAGE_MODULE = "go_standard_annotation_schema"
_JSON_SCHEMA_RESOURCE = "jsonschema/go_standard_annotation_schema.schema.json"


class SchemaArtifactError(RuntimeError):
    """Error raised when a packaged schema artifact is missing or invalid."""


@dataclass(frozen=True, slots=True)
class SchemaArtifacts:
    """Schema data and version supplied by the installed dependency.

    Attributes:
        package_version: Installed go-standard-annotation-schema version.
        json_schema: Parsed Standard Annotation JSON Schema.
    """

    package_version: str
    json_schema: dict[str, object]


def _load_json_schema() -> dict[str, object]:
    """Read and validate the JSON Schema bundled in the installed package.

    Returns:
        Parsed JSON Schema containing the expected schema identifier and
        Annotation definition.

    Raises:
        SchemaArtifactError: If the resource cannot be read or does not have
            the expected JSON structure.
    """
    resource = files(_PACKAGE_MODULE).joinpath(_JSON_SCHEMA_RESOURCE)
    try:
        schema = json.loads(resource.read_bytes())
    except OSError as error:
        raise SchemaArtifactError(
            "Could not read the packaged Standard Annotation JSON Schema"
        ) from error
    except json.JSONDecodeError as error:
        raise SchemaArtifactError(
            "The packaged Standard Annotation JSON Schema is not valid JSON"
        ) from error

    if not isinstance(schema, dict):
        raise SchemaArtifactError(
            "The packaged Standard Annotation JSON Schema must be an object"
        )
    if schema.get("$id") != SCHEMA_ID:
        raise SchemaArtifactError(
            "The packaged Standard Annotation JSON Schema has an unexpected ID"
        )
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict) or "Annotation" not in definitions:
        raise SchemaArtifactError(
            "The packaged Standard Annotation JSON Schema has no Annotation definition"
        )
    return schema


def load_schema_artifacts() -> SchemaArtifacts:
    """Load schema data and version from the installed dependency.

    Returns:
        The installed package version and its bundled JSON Schema.

    Raises:
        SchemaArtifactError: If the bundled JSON Schema is missing or invalid.
    """
    return SchemaArtifacts(
        package_version=version(_PACKAGE_NAME),
        json_schema=_load_json_schema(),
    )
