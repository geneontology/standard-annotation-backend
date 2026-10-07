"""Validate annotation payloads with the schema-generated model."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

from pydantic import ValidationError

from standard_annotation_backend.domain.annotations import Annotation


class ValidationIssue(TypedDict):
    """Serializable details describing one validation failure.

    This type deliberately remains dictionary-shaped because it normalizes Pydantic
    error details into JSON-compatible data. Validation results carry these mappings
    directly into Pydantic API models for serialization; they are not internal value
    objects with behavior or identity.

    Attributes:
        location: Path to the invalid or missing value in the payload.
        message: Human-readable explanation of the failure.
        type: Stable Pydantic identifier for the kind of failure.
    """

    location: tuple[str | int, ...]
    message: str
    type: str


@dataclass(frozen=True, slots=True)
class AnnotationValidationResult:
    """Result of validating an annotation payload.

    Attributes:
        annotation: Validated annotation, or None when validation failed.
        errors: Validation failures; empty when validation succeeded.
    """

    annotation: Annotation | None
    errors: tuple[ValidationIssue, ...]


def validate_annotation(payload: object) -> AnnotationValidationResult:
    """Validate an annotation payload and normalize any validation errors.

    Args:
        payload: JSON-compatible value to validate as a Standard Annotation.

    Returns:
        A result containing either the validated annotation or structured
        validation errors.

    Note:
        Identifier formats declared as LinkML structured patterns are not fully
        enforced until go-standard-annotation-schema is generated with
        LinkML 1.12.0 or newer.
    """
    try:
        annotation = Annotation.model_validate(payload)
    except ValidationError as error:
        return AnnotationValidationResult(
            annotation=None, errors=validation_issues(error)
        )

    return AnnotationValidationResult(annotation=annotation, errors=())


def validation_issues(error: ValidationError) -> tuple[ValidationIssue, ...]:
    """Convert a Pydantic validation error into serializable issues.

    Only each failure's location, message, and type are kept. The rejected input
    value and any error context are left out, so issues never echo submitted data.

    Args:
        error: Validation error raised by a Pydantic model.

    Returns:
        One issue per validation failure, in the order Pydantic reported them.
    """
    return tuple(
        ValidationIssue(
            location=detail["loc"], message=detail["msg"], type=detail["type"]
        )
        for detail in error.errors(
            include_url=False, include_context=False, include_input=False
        )
    )


def field_path(location: Sequence[str | int]) -> str:
    """Return a validation location as a dotted field path.

    Args:
        location: Path to an invalid value, such as `("extensions", 0, "term")`.

    Returns:
        The joined path, such as `extensions.0.term`, or `row` when the location
        is empty because the problem concerns the whole row.
    """
    return ".".join(str(part) for part in location) or "row"
