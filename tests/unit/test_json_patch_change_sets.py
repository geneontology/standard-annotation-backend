"""Tests for applying JSON Patch change-set updates to annotations."""

from copy import deepcopy

import pytest

from standard_annotation_backend.domain.change_sets import (
    InvalidChangeSetPatchError,
    apply_annotation_patch,
)


def annotation_payload() -> dict[str, object]:
    """Return a complete annotation-shaped payload for JSON Patch tests."""
    return {
        "db_object_id": "UniProtKB:P12345",
        "negation": False,
        "relation": "RO:0002331",
        "ontology_class_id": "GO:0008150",
        "references": ["PMID:1"],
        "evidence_type": "ECO:0000314",
        "with_or_from": ["UniProtKB:Q1"],
        "interacting_taxon_id": ["NCBITaxon:9606"],
        "annotation_date": "2026-09-09",
        "assigned_by": "GO_Central",
        "annotation_extensions": [
            {
                "extension_relation": "BFO:0000050",
                "extension_term": "CL:0000000",
            }
        ],
        "annotation_properties": {"comment": ["excluded"]},
    }


def test_replaces_an_unordered_field_as_a_whole_value() -> None:
    """A complete replacement is allowed for an unordered annotation field."""
    result = apply_annotation_patch(
        annotation_payload(),
        [{"op": "replace", "path": "/references", "value": ["PMID:2", "PMID:3"]}],
    )

    assert result["references"] == ["PMID:2", "PMID:3"]


@pytest.mark.parametrize(
    "patch,changed_fields,removed_fields",
    [
        ([{"op": "add", "path": "/negation", "value": True}], {"negation": True}, ()),
        ([{"op": "remove", "path": "/negation"}], {}, ("negation",)),
        (
            [{"op": "replace", "path": "/assigned_by", "value": "MGI"}],
            {"assigned_by": "MGI"},
            (),
        ),
        (
            [{"op": "move", "from": "/assigned_by", "path": "/relation"}],
            {"relation": "GO_Central"},
            ("assigned_by",),
        ),
        (
            [{"op": "copy", "from": "/assigned_by", "path": "/relation"}],
            {"relation": "GO_Central"},
            (),
        ),
        ([{"op": "test", "path": "/assigned_by", "value": "GO_Central"}], {}, ()),
    ],
    ids=["add", "remove", "replace", "move", "copy", "test"],
)
def test_permits_each_supported_rfc_6902_operation(
    patch: object,
    changed_fields: dict[str, object],
    removed_fields: tuple[str, ...],
) -> None:
    """Each RFC 6902 operation has its specified effect on a copy of the input."""
    original = annotation_payload()
    expected = {**original, **changed_fields}
    for field in removed_fields:
        del expected[field]

    result = apply_annotation_patch(original, patch)

    assert result == expected
    assert original == annotation_payload()


@pytest.mark.parametrize(
    "actual,expected",
    [
        (False, 0),
        (True, 1),
        (0, False),
        (1, True),
        (False, 0.0),
        (True, 1.0),
        ([False], [0]),
        ({"flag": True}, {"flag": 1}),
        ({"nested": [{"flag": False}]}, {"nested": [{"flag": 0}]}),
        ([{"nested": [True]}], [{"nested": [1.0]}]),
        (None, False),
        (None, 0),
        ("1", 1),
        ("GO", "go"),
        (1, 2.0),
        ([1, 2], [2, 1]),
        ([1], [1, 2]),
        ({"first": 1}, {"second": 1}),
        ({"first": 1}, {"first": 1, "second": 2}),
        ([], {}),
    ],
)
def test_test_operation_rejects_unequal_json_values(
    actual: object, expected: object
) -> None:
    """Tests compare JSON types and recursively compare complete field values."""
    annotation = {**annotation_payload(), "annotation_properties": actual}
    original = deepcopy(annotation)

    with pytest.raises(InvalidChangeSetPatchError) as raised:
        apply_annotation_patch(
            annotation,
            [
                {"op": "test", "path": "/annotation_properties", "value": expected},
                {"op": "replace", "path": "/assigned_by", "value": "MGI"},
            ],
        )

    assert raised.value.issue.location == ()
    assert raised.value.issue.type == "invalid_patch_operation"
    assert annotation == original


@pytest.mark.parametrize(
    "actual,expected",
    [
        (False, False),
        (True, True),
        (None, None),
        ("GO", "GO"),
        (1, 1.0),
        (1.0, 1),
        (0, -0.0),
        ([], []),
        ({}, {}),
        ([1, {"flag": True, "value": None}], [1.0, {"value": None, "flag": True}]),
        ({"nested": [{"number": 1}]}, {"nested": [{"number": 1.0}]}),
    ],
)
def test_test_operation_accepts_equal_json_values(
    actual: object, expected: object
) -> None:
    """JSON numbers share a type, object order is ignored, and equal tests proceed."""
    annotation = {**annotation_payload(), "annotation_properties": actual}

    result = apply_annotation_patch(
        annotation,
        [
            {"op": "test", "path": "/annotation_properties", "value": expected},
            {"op": "replace", "path": "/assigned_by", "value": "MGI"},
        ],
    )

    assert result["assigned_by"] == "MGI"
    assert annotation["assigned_by"] == "GO_Central"


def test_test_operation_observes_prior_operations() -> None:
    """Each test compares the document produced by the preceding operations."""
    result = apply_annotation_patch(
        annotation_payload(),
        [
            {"op": "replace", "path": "/negation", "value": True},
            {"op": "test", "path": "/negation", "value": True},
            {"op": "replace", "path": "/negation", "value": False},
            {"op": "test", "path": "/negation", "value": False},
        ],
    )

    assert result["negation"] is False


def test_failed_test_after_replacement_keeps_input_unchanged() -> None:
    """A false test after an earlier edit rejects the patch and preserves its input."""
    annotation = annotation_payload()
    original = deepcopy(annotation)

    with pytest.raises(InvalidChangeSetPatchError) as raised:
        apply_annotation_patch(
            annotation,
            [
                {"op": "replace", "path": "/negation", "value": True},
                {"op": "test", "path": "/negation", "value": 1},
                {"op": "replace", "path": "/assigned_by", "value": "MGI"},
            ],
        )

    assert raised.value.issue.location == ()
    assert raised.value.issue.type == "invalid_patch_operation"
    assert annotation == original


@pytest.mark.parametrize(
    "patch",
    [
        [{"op": "test", "path": "/negation"}],
        [
            {"op": "remove", "path": "/negation"},
            {"op": "test", "path": "/negation", "value": None},
        ],
    ],
)
def test_test_operation_requires_a_value_and_existing_field(patch: object) -> None:
    """Missing test values and missing target fields use the stable patch error."""
    with pytest.raises(InvalidChangeSetPatchError) as raised:
        apply_annotation_patch(annotation_payload(), patch)

    assert raised.value.issue.location == ()
    assert raised.value.issue.type == "invalid_patch_operation"


@pytest.mark.parametrize(
    "patch,expected_type,expected_location",
    [
        (None, "patch_must_be_nonempty_list", ()),
        ({}, "patch_must_be_nonempty_list", ()),
        ([], "patch_must_be_nonempty_list", ()),
        (["replace"], "patch_operation_must_be_object", (0,)),
        (
            [{"op": "replace", "path": "/assigned_by"}],
            "invalid_patch_operation",
            (),
        ),
    ],
)
def test_rejects_a_malformed_patch(
    patch: object, expected_type: str, expected_location: tuple[object, ...]
) -> None:
    """Malformed JSON Patch input reports a stable issue type and location."""
    with pytest.raises(InvalidChangeSetPatchError) as raised:
        apply_annotation_patch(annotation_payload(), patch)

    assert raised.value.issue.type == expected_type
    assert raised.value.issue.location == expected_location


@pytest.mark.parametrize(
    "patch,expected_type,expected_location",
    [
        (
            [{"op": "increment", "path": "/assigned_by", "value": "MGI"}],
            "unsupported_patch_operation",
            (0, "op"),
        ),
        (
            [
                {
                    "op": "replace",
                    "path": "/not_a_standard_annotation_field",
                    "value": "x",
                }
            ],
            "unsupported_patch_path",
            (0, "path"),
        ),
    ],
)
def test_rejects_unsupported_operations_and_paths(
    patch: object, expected_type: str, expected_location: tuple[object, ...]
) -> None:
    """Unsupported operations and fields have stable machine-readable failures."""
    with pytest.raises(InvalidChangeSetPatchError) as raised:
        apply_annotation_patch(annotation_payload(), patch)

    assert raised.value.issue.type == expected_type
    assert raised.value.issue.location == expected_location


def test_rejects_a_root_replacement() -> None:
    """A change set cannot replace the complete annotation document."""
    with pytest.raises(InvalidChangeSetPatchError) as raised:
        apply_annotation_patch(
            annotation_payload(),
            [{"op": "replace", "path": "", "value": {}}],
        )

    assert raised.value.issue.location == (0, "path")
    assert raised.value.issue.type == "root_patch_operation_forbidden"


@pytest.mark.parametrize(
    "patch",
    [
        [{"op": "remove", "path": "/references/0"}],
        [{"op": "add", "path": "/references/-", "value": "PMID:2"}],
        [{"op": "remove", "path": "/interacting_taxon_id/0"}],
        [{"op": "remove", "path": "/annotation_properties/comment/0"}],
        [
            {
                "op": "replace",
                "path": "/annotation_extensions/0/extension_term",
                "value": "CL:0000001",
            }
        ],
        [
            {
                "op": "copy",
                "from": "/with_or_from/0",
                "path": "/references",
            }
        ],
        [
            {
                "op": "copy",
                "from": "/interacting_taxon_id/0",
                "path": "/assigned_by",
            }
        ],
        [
            {
                "op": "move",
                "from": "/annotation_properties/comment/0",
                "path": "/assigned_by",
            }
        ],
    ],
)
def test_rejects_positional_or_nested_paths_for_unordered_fields(
    patch: object,
) -> None:
    """Unordered fields cannot be changed through positional or nested pointers."""
    with pytest.raises(InvalidChangeSetPatchError) as raised:
        apply_annotation_patch(annotation_payload(), patch)

    assert raised.value.issue.type == "unsupported_patch_path"
