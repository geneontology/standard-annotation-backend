"""Domain models and business rules for Standard Annotations."""

from standard_annotation_backend.domain.auth import (
    AuthenticationRequiredError,
    AuthorizationContext,
    AuthorizationRole,
    AuthorizationScope,
    PermissionAction,
    PermissionDeniedError,
    RequestContext,
    ResourceOwnership,
    authorize,
    authorize_role,
    derive_creation_group,
)

__all__ = [
    "AuthenticationRequiredError",
    "AuthorizationContext",
    "AuthorizationRole",
    "AuthorizationScope",
    "PermissionAction",
    "PermissionDeniedError",
    "RequestContext",
    "ResourceOwnership",
    "authorize",
    "authorize_role",
    "derive_creation_group",
]
