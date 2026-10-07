"""Apply domain ownership rules to annotation records."""

from uuid import UUID

from standard_annotation_backend.domain.annotations import AnnotationNotFoundError
from standard_annotation_backend.domain.auth import (
    AuthorizationContext,
    AuthorizationScope,
    PermissionAction,
    PermissionDeniedError,
    ResourceOwnership,
    authorize,
)
from standard_annotation_backend.persistence.models import AnnotationRecord
from standard_annotation_backend.persistence.repositories import (
    AnnotationRepository,
    AnnotationSearchFilters,
)


def ownership_filters(context: AuthorizationContext) -> AnnotationSearchFilters:
    """Build annotation query filters for the selected ownership scope.

    Args:
        context: Trusted identity and selected authorization.

    Returns:
        Filters restricting results by group and, for self scope, creator.

    Raises:
        PermissionDeniedError: If a restricted scope has no selected group.
    """
    if context.scope is AuthorizationScope.GLOBAL:
        return AnnotationSearchFilters()
    if context.group_id is None:
        raise PermissionDeniedError
    return AnnotationSearchFilters(
        owning_group_id=context.group_id,
        created_by=context.actor_id
        if context.scope is AuthorizationScope.SELF
        else None,
    )


def authorize_annotation(
    repository: AnnotationRepository,
    context: AuthorizationContext,
    action: PermissionAction,
    record: AnnotationRecord,
) -> None:
    """Require an annotation to fall within the selected ownership scope.

    Callers check the role before resource lookup, allowing this boundary to map
    denied ownership to the same error as a missing annotation. Self scope uses
    the actor recorded on the annotation's first version as its creator.

    Args:
        repository: Repository used to find the first version for self scope.
        context: Trusted identity and selected authorization.
        action: Existing-resource action being attempted.
        record: Current annotation record whose ownership is checked.

    Raises:
        AnnotationNotFoundError: If the annotation is outside the selected scope.
    """
    creator = None
    if context.scope is AuthorizationScope.SELF:
        first_version = repository.get_version(record.annotation_id, 1)
        creator = None if first_version is None else first_version.actor_id
    try:
        authorize(context, action, ResourceOwnership(creator, record.owning_group_id))
    except PermissionDeniedError:
        raise AnnotationNotFoundError(record.annotation_id) from None


def load_authorized_annotation(
    repository: AnnotationRepository,
    context: AuthorizationContext,
    action: PermissionAction,
    annotation_id: UUID,
    *,
    include_deleted: bool = False,
) -> AnnotationRecord:
    """Return an annotation only when it exists within the selected ownership scope.

    A missing annotation and one outside the caller's scope raise the same error,
    so callers cannot learn whether an inaccessible annotation exists. Callers
    check the role with `authorize_role` before calling this function.

    Args:
        repository: Repository used to read the annotation and its first version.
        context: Trusted identity and selected authorization.
        action: Existing-resource action being attempted.
        annotation_id: Identifier of the annotation to load.
        include_deleted: Whether a soft-deleted annotation may be returned.

    Returns:
        The current annotation record.

    Raises:
        AnnotationNotFoundError: If the annotation is missing, is deleted while
            `include_deleted` is false, or is outside the selected scope.
    """
    record = repository.get(annotation_id, include_deleted=include_deleted)
    if record is None:
        raise AnnotationNotFoundError(annotation_id)
    authorize_annotation(repository, context, action, record)
    return record
