"""Apply conservative JSON Patch updates to Standard Annotations."""

from copy import deepcopy
from dataclasses import dataclass

import jsonpatch

from standard_annotation_backend.domain.annotations import Annotation


@dataclass(frozen=True, slots=True)
class ChangeSetPatchIssue:
    """Describe a trusted validation failure in a change-set patch.

    Patch validation creates this internal value after inspecting untrusted input.
    It is not a request model or serialized API schema.

    Attributes:
        location: Location of the invalid value within the patch document.
        type: Stable identifier for the kind of invalid patch input.
    """

    location: tuple[int | str, ...]
    type: str


class InvalidChangeSetPatchError(ValueError):
    """Report a patch that cannot safely update a Standard Annotation.

    Attributes:
        issue: Stable machine-readable information about the invalid patch.
    """

    def __init__(self, issue: ChangeSetPatchIssue) -> None:
        self.issue = issue
        super().__init__("change-set patch is invalid")


_ALLOWED_OPERATIONS = frozenset({"add", "remove", "replace", "move", "copy", "test"})
_ANNOTATION_FIELDS = frozenset(Annotation.model_fields)
_ANNOTATION_FIELD_POINTERS = frozenset(f"/{field}" for field in _ANNOTATION_FIELDS)
_OPERATIONS_REQUIRING_FROM = frozenset({"move", "copy"})


def apply_annotation_patch(
    annotation: dict[str, object], patch: object
) -> dict[str, object]:
    """Apply a valid JSON Patch to a copy of an annotation payload.

    The patch can operate only on complete top-level Standard Annotation fields.
    This prevents review-ambiguous positional edits to unordered fields and keeps
    structural annotation validation in the service layer.
    Tests use recursive JSON equality, which keeps booleans distinct from numbers.

    Args:
        annotation: Existing Standard Annotation payload to patch.
        patch: Untrusted JSON Patch document.

    Returns:
        A patched copy of `annotation`.

    Raises:
        InvalidChangeSetPatchError: If the patch is malformed, targets an
            unsupported path, or cannot be applied to the annotation.
    """
    _validate_patch(patch)

    result = deepcopy(annotation)
    try:
        for operation in jsonpatch.JsonPatch(patch):
            if operation["op"] == "test":
                field = operation["path"][1:]
                if (
                    "value" not in operation
                    or field not in result
                    or not _json_values_equal(result[field], operation["value"])
                ):
                    raise jsonpatch.JsonPatchTestFailed
            else:
                result = jsonpatch.JsonPatch([operation]).apply(result, in_place=True)
    except jsonpatch.JsonPatchException:
        raise InvalidChangeSetPatchError(
            ChangeSetPatchIssue(location=(), type="invalid_patch_operation")
        ) from None

    assert isinstance(result, dict)
    return result


def _json_values_equal(left: object, right: object) -> bool:
    """Compare JSON values recursively with the type rules used by JSON Patch."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_values_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_values_equal(left[key], right[key]) for key in left
        )
    return type(left) is type(right) and left == right


def _validate_patch(patch: object) -> None:
    """Validate patch structure and SAB's annotation path policy."""
    if not isinstance(patch, list) or not patch:
        raise InvalidChangeSetPatchError(
            ChangeSetPatchIssue(location=(), type="patch_must_be_nonempty_list")
        )

    for index, operation in enumerate(patch):
        if not isinstance(operation, dict):
            raise InvalidChangeSetPatchError(
                ChangeSetPatchIssue(
                    location=(index,), type="patch_operation_must_be_object"
                )
            )

        _validate_operation(index, operation)


def _validate_operation(index: int, operation: dict[object, object]) -> None:
    """Validate one JSON Patch operation before it reaches the dependency."""
    op = operation.get("op")
    if not isinstance(op, str):
        raise InvalidChangeSetPatchError(
            ChangeSetPatchIssue(location=(index, "op"), type="invalid_patch_operation")
        )
    if op not in _ALLOWED_OPERATIONS:
        raise InvalidChangeSetPatchError(
            ChangeSetPatchIssue(
                location=(index, "op"), type="unsupported_patch_operation"
            )
        )

    _validate_pointer(index, "path", operation.get("path"))

    from_path = operation.get("from")
    if op in _OPERATIONS_REQUIRING_FROM and from_path is None:
        raise InvalidChangeSetPatchError(
            ChangeSetPatchIssue(
                location=(index, "from"), type="invalid_patch_operation"
            )
        )
    if from_path is not None:
        _validate_pointer(index, "from", from_path)


def _validate_pointer(index: int, member: str, pointer: object) -> None:
    """Validate that a pointer names one complete Standard Annotation field."""
    if not isinstance(pointer, str):
        raise InvalidChangeSetPatchError(
            ChangeSetPatchIssue(location=(index, member), type="invalid_patch_path")
        )
    if pointer == "":
        raise InvalidChangeSetPatchError(
            ChangeSetPatchIssue(
                location=(index, member), type="root_patch_operation_forbidden"
            )
        )
    if pointer not in _ANNOTATION_FIELD_POINTERS:
        raise InvalidChangeSetPatchError(
            ChangeSetPatchIssue(location=(index, member), type="unsupported_patch_path")
        )
