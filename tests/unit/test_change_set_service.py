"""Test change-set request validation without database access."""

from importlib import import_module
from typing import NoReturn, cast
from uuid import uuid4

import pytest

from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
    PermissionDeniedError,
    RequestContext,
)
from standard_annotation_backend.domain.change_sets import (
    InvalidChangeSetError,
)


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
    with pytest.raises(InvalidChangeSetError) as error:
        service.propose_create(
            payload=payload,
            owning_group_id=group,
            reason=reason,
            context=RequestContext(
                actor_id="proposer",
                token_id=uuid4(),
                token_name="Test proposer",
                role=AuthorizationRole.EDIT,
                scope=AuthorizationScope.GLOBAL,
                group_id=None,
            ),
        )
    assert error.value.errors


@pytest.mark.parametrize("reason", ["", " ", "\t\n"])
def test_rejection_requires_nonblank_reason_without_opening_transaction(
    reason: str,
) -> None:
    """A blank review explanation cannot start a rejection workflow."""
    module = import_module("standard_annotation_backend.services.change_set_service")
    service = module.ChangeSetService(_unopened_transaction)
    with pytest.raises(InvalidChangeSetError) as error:
        service.reject(
            uuid4(),
            review_reason=reason,
            context=RequestContext(
                actor_id="reviewer",
                token_id=uuid4(),
                token_name="Test reviewer",
                role=AuthorizationRole.ADMIN,
                scope=AuthorizationScope.GLOBAL,
                group_id=None,
            ),
        )
    assert error.value.errors[0]["location"] == ("review_reason",)


@pytest.mark.parametrize(
    "operation,role",
    [
        ("propose_create", AuthorizationRole.READ),
        ("propose_update", AuthorizationRole.READ),
        ("propose_delete", AuthorizationRole.READ),
        ("preview", AuthorizationRole.READ),
        ("accept", AuthorizationRole.READ),
        ("accept", AuthorizationRole.EDIT),
        ("reject", AuthorizationRole.READ),
        ("reject", AuthorizationRole.EDIT),
    ],
)
def test_insufficient_change_set_role_denies_before_opening_transaction(
    operation: str,
    role: AuthorizationRole,
) -> None:
    """Proposals and persisted previews require edit; reviews require admin."""
    module = import_module("standard_annotation_backend.services.change_set_service")
    service = module.ChangeSetService(_unopened_transaction)
    context = RequestContext(
        actor_id="actor",
        token_id=uuid4(),
        token_name="Read token",
        role=role,
        scope=AuthorizationScope.GROUP,
        group_id="group",
    )
    with pytest.raises(PermissionDeniedError):
        if operation == "propose_create":
            service.propose_create(
                payload={}, owning_group_id="group", reason="Review", context=context
            )
        elif operation.startswith("propose_"):
            getattr(service, operation)(
                uuid4(),
                base_version=1,
                reason="Review",
                context=context,
                **(
                    {"patch": [{"op": "remove", "path": "/assigned_by"}]}
                    if operation == "propose_update"
                    else {}
                ),
            )
        elif operation == "reject":
            service.reject(uuid4(), review_reason="Review", context=context)
        else:
            getattr(service, operation)(uuid4(), context=context)


@pytest.mark.parametrize("version", [0, -1, True, "1"])
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_proposals_require_strict_positive_base_version(
    operation: str, version: object
) -> None:
    """Invalid version metadata fails before a repository transaction is opened."""
    module = import_module("standard_annotation_backend.services.change_set_service")
    service = module.ChangeSetService(_unopened_transaction)
    with pytest.raises(InvalidChangeSetError) as error:
        getattr(service, f"propose_{operation}")(
            uuid4(),
            base_version=version,
            reason="Review",
            context=RequestContext(
                actor_id="proposer",
                token_id=uuid4(),
                token_name="Test proposer",
                role=AuthorizationRole.EDIT,
                scope=AuthorizationScope.GLOBAL,
                group_id=None,
            ),
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
    with pytest.raises(InvalidChangeSetError) as error:
        service.accept(
            uuid4(),
            review_reason=cast(str, reason),
            context=RequestContext(
                actor_id="reviewer",
                token_id=uuid4(),
                token_name="Test reviewer",
                role=AuthorizationRole.ADMIN,
                scope=AuthorizationScope.GLOBAL,
                group_id=None,
            ),
        )
    assert error.value.errors[0]["location"] == ("review_reason",)
