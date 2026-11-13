"""Read data packaged with the `go-standard-annotation-schema` dependency."""

from __future__ import annotations

import json
from importlib.resources import files

import yaml
from go_standard_annotation_schema import __version__ as _installed_schema_version
from go_standard_annotation_schema.datamodel.go_standard_annotation_schema import (
    EvidenceType,
)
from pydantic import BaseModel, ValidationError

SCHEMA_ID = "https://w3id.org/geneontology/go-standard-annotation-schema"
SCHEMA_PACKAGE_VERSION: str = _installed_schema_version
"""Version of the installed `go-standard-annotation-schema` package, such as `0.1.0`.

SABE checks annotations against this package's models. Each GPAD import records
the version it was checked with, and a refresh imports an unchanged file again when
the recorded version differs from this one.
"""
_PACKAGE_MODULE = "go_standard_annotation_schema"
_JSON_SCHEMA_RESOURCE = "jsonschema/go_standard_annotation_schema.schema.json"
_EVIDENCE_VALUESET_RESOURCE = "schema/valuesets/evidence.yaml"


class SchemaArtifactError(RuntimeError):
    """Error raised when a packaged schema artifact is missing or invalid."""


def load_json_schema() -> dict[str, object]:
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


class _PermissibleValue(BaseModel):
    """Read one allowed value of an enum in a LinkML value set file.

    Only `aliases` is read. Other keys, such as `title` and `meaning`, are ignored.
    """

    aliases: list[str]


class _ValueSetEnum(BaseModel):
    """Read one enum in a LinkML value set file, with its allowed values by name."""

    permissible_values: dict[str, _PermissibleValue]


class _ValueSet(BaseModel):
    """Read the enums defined in a LinkML value set file, by enum name."""

    enums: dict[str, _ValueSetEnum]


def load_evidence_codes() -> dict[str, str]:
    """Return the GO evidence code for each evidence term the schema allows.

    The schema's `EvidenceType` enum allows one ECO term for each GO evidence code.
    Its value set file, `schema/valuesets/evidence.yaml`, lists the code as the
    term's only alias. GO Rules refer to evidence by code, so SABE reads the codes
    from that file rather than keeping its own copy.

    Returns:
        A mapping from each allowed ECO identifier to its GO evidence code, such as
        `ECO:0000353` to `IPI`.

    Raises:
        SchemaArtifactError: If the file cannot be read or parsed, a term does not
            have exactly one alias, the file's terms differ from the `EvidenceType`
            values, or two terms share a code.
    """
    resource = files(_PACKAGE_MODULE).joinpath(_EVIDENCE_VALUESET_RESOURCE)
    try:
        valueset = _ValueSet.model_validate(yaml.safe_load(resource.read_bytes()))
    except OSError as error:
        raise SchemaArtifactError(
            "Could not read the packaged evidence value set"
        ) from error
    except (yaml.YAMLError, ValidationError) as error:
        raise SchemaArtifactError(
            "The packaged evidence value set does not have the expected structure"
        ) from error
    evidence_enum = valueset.enums.get("EvidenceType")
    if evidence_enum is None:
        raise SchemaArtifactError("The evidence value set has no EvidenceType enum")
    codes: dict[str, str] = {}
    for term, value in evidence_enum.permissible_values.items():
        if len(value.aliases) != 1:
            raise SchemaArtifactError(
                "Each evidence term must have exactly one GO evidence code alias"
            )
        codes[term] = value.aliases[0]
    if set(codes) != {member.value for member in EvidenceType}:
        raise SchemaArtifactError(
            "The evidence value set does not match the schema's EvidenceType values"
        )
    if len(set(codes.values())) != len(codes):
        raise SchemaArtifactError("Two evidence terms share a GO evidence code")
    return codes
