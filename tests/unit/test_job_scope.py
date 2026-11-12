"""Verify how job parameters name one owning group or all groups."""

import pytest

from standard_annotation_backend.domain.jobs import group_scope, group_scope_parameters


@pytest.mark.parametrize("group_key", [None, "MGI"])
def test_scope_parameters_round_trip(group_key: str | None) -> None:
    """All groups is `{}`; one group is `{"group_key": ...}`."""
    assert group_scope(group_scope_parameters(group_key)) == group_key


@pytest.mark.parametrize(
    "parameters",
    [{"group_key": ""}, {"group_key": 5}, {"group_key": " MGI"}, {"source_key": "mgi"}],
)
def test_other_scope_parameters_are_rejected(parameters: dict[str, object]) -> None:
    """Any other parameters are invalid."""
    with pytest.raises(ValueError, match="job scope parameters"):
        group_scope(parameters)
