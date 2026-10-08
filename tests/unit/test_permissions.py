"""Tests for authorization decision domain logic."""

import pytest

from standard_annotation_backend.domain.auth import (
    AuthorizationContext,
    AuthorizationRole,
    AuthorizationScope,
    PermissionAction,
    PermissionDeniedError,
    ResourceOwnership,
    authorize,
    authorize_role,
    derive_creation_group,
)


def _context(
    role: AuthorizationRole,
    scope: AuthorizationScope,
) -> AuthorizationContext:
    return AuthorizationContext(
        actor_id="curator-1",
        role=role,
        scope=scope,
        group_id=None if scope is AuthorizationScope.GLOBAL else "group-1",
    )


MATCHING_RESOURCE = ResourceOwnership(
    created_by="curator-1",
    owning_group_id="group-1",
)


@pytest.mark.parametrize(
    ("role", "scope", "action", "permitted"),
    [
        ("read", "self", "annotation.read", True),
        ("read", "self", "annotation.edit", False),
        ("read", "self", "change_set.review", False),
        ("read", "group", "annotation.read", True),
        ("read", "group", "annotation.edit", False),
        ("read", "group", "change_set.review", False),
        ("read", "global", "annotation.read", True),
        ("read", "global", "annotation.edit", False),
        ("read", "global", "change_set.review", False),
        ("edit", "self", "annotation.read", True),
        ("edit", "self", "annotation.edit", True),
        ("edit", "self", "change_set.review", False),
        ("edit", "group", "annotation.read", True),
        ("edit", "group", "annotation.edit", True),
        ("edit", "group", "change_set.review", False),
        ("edit", "global", "annotation.read", True),
        ("edit", "global", "annotation.edit", True),
        ("edit", "global", "change_set.review", False),
        ("admin", "self", "annotation.read", True),
        ("admin", "self", "annotation.edit", True),
        ("admin", "self", "change_set.review", True),
        ("admin", "group", "annotation.read", True),
        ("admin", "group", "annotation.edit", True),
        ("admin", "group", "change_set.review", True),
        ("admin", "global", "annotation.read", True),
        ("admin", "global", "annotation.edit", True),
        ("admin", "global", "change_set.review", True),
    ],
)
def test_role_and_scope_matrix_has_literal_permission_outcomes(
    role: str,
    scope: str,
    action: str,
    permitted: bool,
) -> None:
    context = _context(AuthorizationRole(role), AuthorizationScope(scope))

    if permitted:
        authorize(context, PermissionAction(action), MATCHING_RESOURCE)
    else:
        with pytest.raises(PermissionDeniedError, match=r"^Permission denied$"):
            authorize(context, PermissionAction(action), MATCHING_RESOURCE)


@pytest.mark.parametrize("role", list(AuthorizationRole))
@pytest.mark.parametrize("scope", list(AuthorizationScope))
def test_only_global_admins_may_start_refreshes(
    role: AuthorizationRole, scope: AuthorizationScope
) -> None:
    """Only the admin role with global scope may create refresh jobs."""
    context = _context(role, scope)

    if role is AuthorizationRole.ADMIN and scope is AuthorizationScope.GLOBAL:
        authorize_role(context, PermissionAction.REFRESH_CREATE)
    else:
        with pytest.raises(PermissionDeniedError):
            authorize_role(context, PermissionAction.REFRESH_CREATE)


@pytest.mark.parametrize(
    "action",
    [
        PermissionAction.ANNOTATION_READ,
        PermissionAction.CHANGE_SET_READ,
    ],
)
def test_read_role_allows_each_read_action(action: PermissionAction) -> None:
    authorize(
        _context(AuthorizationRole.READ, AuthorizationScope.GROUP),
        action,
        MATCHING_RESOURCE,
    )


@pytest.mark.parametrize("role", [AuthorizationRole.READ, AuthorizationRole.EDIT])
def test_job_reads_require_admin_role(role: AuthorizationRole) -> None:
    """Non-admin roles cannot read system-wide job state."""
    with pytest.raises(PermissionDeniedError, match=r"^Permission denied$"):
        authorize(
            _context(role, AuthorizationScope.GLOBAL),
            PermissionAction.JOB_READ,
            MATCHING_RESOURCE,
        )

    authorize(
        _context(AuthorizationRole.ADMIN, AuthorizationScope.GLOBAL),
        PermissionAction.JOB_READ,
        MATCHING_RESOURCE,
    )


@pytest.mark.parametrize(
    "action",
    [
        PermissionAction.ANNOTATION_EDIT,
        PermissionAction.ANNOTATION_DELETE,
        PermissionAction.CHANGE_SET_PROPOSE,
    ],
)
def test_edit_role_allows_each_edit_action(action: PermissionAction) -> None:
    authorize(
        _context(AuthorizationRole.EDIT, AuthorizationScope.GROUP),
        action,
        MATCHING_RESOURCE,
    )


@pytest.mark.parametrize(
    "resource",
    [
        ResourceOwnership(created_by="other-user", owning_group_id="group-1"),
        ResourceOwnership(created_by="curator-1", owning_group_id="other-group"),
        ResourceOwnership(created_by=None, owning_group_id="group-1"),
    ],
)
def test_self_scope_requires_matching_creator_and_group(
    resource: ResourceOwnership,
) -> None:
    context = _context(AuthorizationRole.EDIT, AuthorizationScope.SELF)

    with pytest.raises(PermissionDeniedError, match=r"^Permission denied$"):
        authorize(context, PermissionAction.ANNOTATION_EDIT, resource)


@pytest.mark.parametrize(
    "resource",
    [
        ResourceOwnership(created_by="curator-1", owning_group_id="other-group"),
        ResourceOwnership(created_by="curator-1", owning_group_id=None),
    ],
)
def test_group_scope_requires_matching_owning_group(
    resource: ResourceOwnership,
) -> None:
    context = _context(AuthorizationRole.EDIT, AuthorizationScope.GROUP)

    with pytest.raises(PermissionDeniedError, match=r"^Permission denied$"):
        authorize(context, PermissionAction.ANNOTATION_EDIT, resource)


@pytest.mark.parametrize("scope", [AuthorizationScope.SELF, AuthorizationScope.GROUP])
@pytest.mark.parametrize("requested_group", [None, "group-1"])
def test_group_restricted_creation_derives_ownership(
    scope: AuthorizationScope,
    requested_group: str | None,
) -> None:
    context = _context(AuthorizationRole.EDIT, scope)

    assert (
        derive_creation_group(
            context,
            requested_group,
            action=PermissionAction.ANNOTATION_CREATE,
        )
        == "group-1"
    )


@pytest.mark.parametrize("scope", [AuthorizationScope.SELF, AuthorizationScope.GROUP])
def test_group_restricted_creation_rejects_conflicting_ownership(
    scope: AuthorizationScope,
) -> None:
    context = _context(AuthorizationRole.EDIT, scope)

    with pytest.raises(PermissionDeniedError, match=r"^Permission denied$"):
        derive_creation_group(
            context,
            "other-group",
            action=PermissionAction.ANNOTATION_CREATE,
        )


def test_global_creation_requires_and_preserves_explicit_ownership() -> None:
    context = _context(AuthorizationRole.EDIT, AuthorizationScope.GLOBAL)

    with pytest.raises(PermissionDeniedError, match=r"^Permission denied$"):
        derive_creation_group(context, None, action=PermissionAction.ANNOTATION_CREATE)

    assert (
        derive_creation_group(
            context,
            "requested-group",
            action=PermissionAction.ANNOTATION_CREATE,
        )
        == "requested-group"
    )


def test_creation_ownership_enforces_role_before_scope() -> None:
    context = _context(AuthorizationRole.READ, AuthorizationScope.GLOBAL)

    with pytest.raises(PermissionDeniedError, match=r"^Permission denied$"):
        derive_creation_group(
            context,
            "requested-group",
            action=PermissionAction.ANNOTATION_CREATE,
        )


@pytest.mark.parametrize(
    "role,action,permitted",
    [
        (AuthorizationRole.READ, PermissionAction.ANNOTATION_READ, True),
        (AuthorizationRole.READ, PermissionAction.ANNOTATION_EDIT, False),
        (AuthorizationRole.EDIT, PermissionAction.CHANGE_SET_PROPOSE, True),
        (AuthorizationRole.EDIT, PermissionAction.CHANGE_SET_REVIEW, False),
        (AuthorizationRole.ADMIN, PermissionAction.CHANGE_SET_REVIEW, True),
    ],
)
def test_role_decision_can_precede_resource_lookup(
    role: AuthorizationRole,
    action: PermissionAction,
    permitted: bool,
) -> None:
    """Role denial is independent of whether a targeted resource exists."""
    context = _context(role, AuthorizationScope.SELF)
    if permitted:
        authorize_role(context, action)
    else:
        with pytest.raises(PermissionDeniedError):
            authorize_role(context, action)
