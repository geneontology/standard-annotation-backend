"""Validate annotation payloads with the schema-generated model."""

from dataclasses import dataclass
from typing import TypedDict

from pydantic import ValidationError

from standard_annotation_backend.domain.annotations import Annotation


class ValidationIssue(TypedDict):
    """Serializable details describing one validation failure.

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

    @property
    def is_valid(self) -> bool:
        """Return whether validation produced an annotation.

        Returns:
            True when annotation is populated; otherwise, False.
        """
        return self.annotation is not None


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
        issues: list[ValidationIssue] = []
        for detail in error.errors(include_url=False, include_context=False):
            issue = ValidationIssue(
                location=detail["loc"],
                message=detail["msg"],
                type=detail["type"],
            )
            issues.append(issue)
        return AnnotationValidationResult(annotation=None, errors=tuple(issues))

    return AnnotationValidationResult(annotation=annotation, errors=())
