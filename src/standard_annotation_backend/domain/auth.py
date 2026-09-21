"""Make transport-neutral authentication, authorization, and ownership decisions."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class AuthorizationRole(StrEnum):
    """Describe which kinds of application actions an identity may perform."""

    READ = "read"
    EDIT = "edit"
    ADMIN = "admin"


class AuthorizationScope(StrEnum):
    """Describe which owned resources an authorization applies to."""

    SELF = "self"
    GROUP = "group"
    GLOBAL = "global"


class PermissionAction(StrEnum):
    """Name an application action that requires authorization."""

    ANNOTATION_READ = "annotation.read"
    ANNOTATION_CREATE = "annotation.create"
    ANNOTATION_EDIT = "annotation.edit"
    ANNOTATION_DELETE = "annotation.delete"
    ANNOTATION_COMMENT_ADMIN = "annotation_comment.admin"
    CHANGE_SET_READ = "change_set.read"
    CHANGE_SET_PROPOSE = "change_set.propose"
    CHANGE_SET_REVIEW = "change_set.review"


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    """Describe the trusted authorization selected for an authenticated actor.

    Attributes:
        actor_id: Stable identifier for the authenticated user.
        role: Kind of action the selected authorization permits.
        scope: Ownership boundary for the selected authorization.
        group_id: Required group for self and group scopes, or `None` for global.
    """

    actor_id: str
    role: AuthorizationRole
    scope: AuthorizationScope
    group_id: str | None


@dataclass(frozen=True, slots=True)
class RequestContext(AuthorizationContext):
    """Identify an authenticated user, selected authorization, and bearer token.

    Attributes:
        token_id: Stable identifier of the token used for this request.
        token_name: Human-readable token name for attribution.
    """

    token_id: UUID
    token_name: str


@dataclass(frozen=True, slots=True)
class ResourceOwnership:
    """Describe ownership values used to authorize an existing resource.

    Attributes:
        created_by: Stable identifier for the actor that created the resource.
        owning_group_id: Identifier for the group that owns the resource.
    """

    created_by: str | None
    owning_group_id: str | None


class AuthenticationRequiredError(Exception):
    """Report that an authenticated identity is required without disclosing details."""

    def __init__(self) -> None:
        super().__init__("authentication required")


class PermissionDeniedError(Exception):
    """Report a denied authorization decision without disclosing details."""

    def __init__(self) -> None:
        super().__init__("permission denied")


_READ_ACTIONS = frozenset(
    {
        PermissionAction.ANNOTATION_READ,
        PermissionAction.CHANGE_SET_READ,
    }
)
_EDIT_ACTIONS = frozenset(
    {
        PermissionAction.ANNOTATION_CREATE,
        PermissionAction.ANNOTATION_EDIT,
        PermissionAction.ANNOTATION_DELETE,
        PermissionAction.CHANGE_SET_PROPOSE,
    }
)


def authorize(
    context: AuthorizationContext,
    action: PermissionAction,
    resource: ResourceOwnership,
) -> None:
    """Require the selected role and scope to permit an existing-resource action.

    Args:
        context: Trusted identity and selected authorization.
        action: Application action being attempted.
        resource: Ownership of the existing resource.

    Raises:
        PermissionDeniedError: If the role or ownership scope denies the action.
    """
    authorize_role(context, action)
    if not _scope_permits(context, resource):
        raise PermissionDeniedError


def authorize_role(context: AuthorizationContext, action: PermissionAction) -> None:
    """Require an action's role independently of resource ownership.

    For existing resources, callers must still check ownership with `authorize`.
    Separating the checks lets callers deny a role before looking up a potentially
    private resource, without disclosing whether it exists.

    Args:
        context: Trusted identity and selected authorization.
        action: Application action being attempted.

    Raises:
        PermissionDeniedError: If the selected role cannot perform the action.
    """
    if not _role_permits(context.role, action):
        raise PermissionDeniedError


def derive_creation_group(
    context: AuthorizationContext,
    requested_group: str | None,
    *,
    action: PermissionAction,
) -> str:
    """Determine annotation ownership for a creation request.

    Group-restricted contexts supply their selected group and reject a conflicting
    requested group. Global contexts require the caller to choose ownership.

    Args:
        context: Trusted identity and selected authorization.
        requested_group: Optional ownership supplied at the application boundary.
        action: Creation operation whose role must be permitted.

    Returns:
        The group that must own the new annotation.

    Raises:
        PermissionDeniedError: If creation is not permitted or ownership conflicts.
    """
    authorize_role(context, action)

    if context.scope is AuthorizationScope.GLOBAL:
        if requested_group is None:
            raise PermissionDeniedError
        return requested_group

    if context.group_id is None:
        raise PermissionDeniedError
    if requested_group is not None and requested_group != context.group_id:
        raise PermissionDeniedError
    return context.group_id


def _role_permits(role: AuthorizationRole, action: PermissionAction) -> bool:
    """Return whether a role permits an action before ownership is considered.

    Args:
        role: Selected authorization role.
        action: Application action being attempted.

    Returns:
        Whether the role permits the action.
    """
    if action in _READ_ACTIONS:
        return True
    if action in _EDIT_ACTIONS:
        return role in {AuthorizationRole.EDIT, AuthorizationRole.ADMIN}
    return role is AuthorizationRole.ADMIN


def _scope_permits(
    context: AuthorizationContext,
    resource: ResourceOwnership,
) -> bool:
    """Return whether an existing resource falls within the selected scope.

    Args:
        context: Trusted identity and selected authorization.
        resource: Ownership of the existing resource.

    Returns:
        Whether the selected scope includes the resource.
    """
    if context.scope is AuthorizationScope.GLOBAL:
        return True
    if context.group_id is None or resource.owning_group_id is None:
        return False
    if resource.owning_group_id != context.group_id:
        return False
    if context.scope is AuthorizationScope.SELF:
        return (
            resource.created_by is not None and resource.created_by == context.actor_id
        )
    return True
