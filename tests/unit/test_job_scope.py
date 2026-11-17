"""Verify how job parameters name one owning group or all groups."""

import pytest

from standard_annotation_backend.domain.jobs import scan_group_parameters, scan_groups


@pytest.mark.parametrize(
    ("group_key", "parameters"), [(None, {}), ("MGI", {"group_key": "MGI"})]
)
def test_scan_group_parameters_round_trip(
    group_key: str | None, parameters: dict[str, object]
) -> None:
    """All groups is stored as `{}` and one group as `{"group_key": ...}`.

    Both read back as the groups they were written from.
    """
    assert scan_group_parameters(group_key) == parameters
    assert scan_groups(parameters) == group_key


@pytest.mark.parametrize(
    "parameters",
    [{"group_key": ""}, {"group_key": 5}, {"group_key": " MGI"}, {"source_key": "mgi"}],
)
def test_other_scan_group_parameters_are_rejected(
    parameters: dict[str, object],
) -> None:
    """Any other parameters are invalid."""
    with pytest.raises(ValueError, match="scan group parameters"):
        scan_groups(parameters)
