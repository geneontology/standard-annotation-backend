"""Verify that API issues carry every details shape unchanged."""

import pytest

from standard_annotation_backend.api.models import ApiValidationIssue

ISSUE = {"location": ["annotation"], "message": "x", "type": "t"}


@pytest.mark.parametrize(
    "details",
    [
        {"replaced_by": ["GO:1"], "consider": []},
        {"primary_id": "GO_REF:0000002"},
        {"peer_ids": ["00000000-0000-0000-0000-000000000001"]},
        {"allowed_relations": ["RO:0002327", "RO:0002331"]},
    ],
)
def test_every_details_shape_is_returned_unchanged(details: dict) -> None:
    """Each shape SABE reports, or has stored in QC findings, serializes as is."""
    issue = ApiValidationIssue.model_validate({**ISSUE, "details": details})

    assert issue.model_dump(mode="json")["details"] == details


def test_an_issue_without_details_has_no_details_key() -> None:
    """Details are omitted, not null, when an issue has none."""
    issue = ApiValidationIssue.model_validate(ISSUE)

    assert "details" not in issue.model_dump(mode="json")
