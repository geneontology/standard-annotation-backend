"""Test change-set request validation without database access."""

from importlib import import_module
from typing import NoReturn, cast
from uuid import uuid4

import pytest

from standard_annotation_backend.services.annotation_service import RequestContext


def _unopened_transaction() -> NoReturn:
    raise AssertionError("invalid request must not open a transaction")


@pytest.mark.parametrize(
    "payload,group,reason",
    [
        ([], "group", "reason"),
        ({}, "", "reason"),
        ({}, "  ", "reason"),
        ({}, "group", "\t "),
        ({"value": object()}, "group", "reason"),
    ],
)
def test_invalid_create_envelope_is_rejected_before_transaction(
    payload: object,
    group: str,
    reason: str,
) -> None:
    """Malformed JSON payloads and blank proposal metadata never reach storage."""
    module = import_module("standard_annotation_backend.services.change_set_service")
    service = module.ChangeSetService(_unopened_transaction)
    with pytest.raises(module.InvalidChangeSetError) as error:
        service.propose_create(
            payload=payload,
            owning_group_id=group,
            reason=reason,
            context=RequestContext(actor_id="proposer"),
        )
    assert error.value.errors


@pytest.mark.parametrize("reason", ["", " ", "\t\n"])
def test_rejection_requires_nonblank_reason_without_opening_transaction(
    reason: str,
) -> None:
    """A blank review explanation cannot start a rejection workflow."""
    module = import_module("standard_annotation_backend.services.change_set_service")
    service = module.ChangeSetService(_unopened_transaction)
    with pytest.raises(module.InvalidChangeSetError) as error:
        service.reject(
            uuid4(), review_reason=reason, context=RequestContext(actor_id="reviewer")
        )
    assert error.value.errors[0]["location"] == ("review_reason",)


@pytest.mark.parametrize("version", [0, -1, True, "1"])
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_proposals_require_strict_positive_base_version(
    operation: str, version: object
) -> None:
    """Invalid version metadata fails before a repository transaction is opened."""
    module = import_module("standard_annotation_backend.services.change_set_service")
    service = module.ChangeSetService(_unopened_transaction)
    with pytest.raises(module.InvalidChangeSetError) as error:
        getattr(service, f"propose_{operation}")(
            uuid4(),
            base_version=version,
            reason="Review",
            context=RequestContext(actor_id="proposer"),
            **(
                {"patch": [{"op": "remove", "path": "/assigned_by"}]}
                if operation == "update"
                else {}
            ),
        )
    assert error.value.errors[0]["location"] == ("base_version",)


@pytest.mark.parametrize("reason", [42, {"text": "reviewed"}])
def test_acceptance_rejects_nontext_review_reason_before_transaction(
    reason: object,
) -> None:
    """Malformed optional review metadata cannot reach the acceptance transaction."""
    module = import_module("standard_annotation_backend.services.change_set_service")
    service = module.ChangeSetService(_unopened_transaction)
    with pytest.raises(module.InvalidChangeSetError) as error:
        service.accept(
            uuid4(),
            review_reason=cast(str, reason),
            context=RequestContext(actor_id="reviewer"),
        )
    assert error.value.errors[0]["location"] == ("review_reason",)
