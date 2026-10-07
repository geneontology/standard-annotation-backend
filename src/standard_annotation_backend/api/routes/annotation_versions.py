"""Expose HTTP operations for reading saved annotation versions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, status

from standard_annotation_backend.api.dependencies import (
    PageLimit,
    PageOffset,
    get_annotation_service,
    get_authenticated_context,
)
from standard_annotation_backend.api.errors import BEARER_ERROR_RESPONSES
from standard_annotation_backend.api.models import (
    AnnotationVersionPageResponse,
    AnnotationVersionResource,
    ApiErrorResponse,
)
from standard_annotation_backend.domain.auth import RequestContext
from standard_annotation_backend.services.annotation_service import AnnotationService

router = APIRouter(
    prefix="/annotations",
    tags=["annotations"],
    dependencies=[Depends(get_authenticated_context)],
    responses={
        **BEARER_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ApiErrorResponse},
    },
)


@router.get(
    "/{annotation_id}/versions",
    response_model=AnnotationVersionPageResponse,
)
def list_annotation_versions(
    annotation_id: UUID,
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
    limit: PageLimit = 50,
    offset: PageOffset = 0,
) -> AnnotationVersionPageResponse:
    """Return saved annotation versions from oldest to newest.

    Args:
        annotation_id: Identifier of the annotation whose history is requested.
        service: Annotation operations for this request.
        context: Authenticated identity and current annotation ownership scope.
        limit: Maximum number of versions to return.
        offset: Number of versions to skip.

    Returns:
        The requested versions and pagination information.
    """
    return AnnotationVersionPageResponse.from_service(
        service.list_versions(
            annotation_id, limit=limit, offset=offset, context=context
        )
    )


@router.get(
    "/{annotation_id}/versions/{version}",
    response_model=AnnotationVersionResource,
)
def get_annotation_version(
    annotation_id: UUID,
    version: Annotated[int, Path(ge=1)],
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> AnnotationVersionResource:
    """Return the annotation data and change information saved in one version.

    Args:
        annotation_id: Identifier of the annotation to inspect.
        version: Positive version number to retrieve.
        service: Annotation operations for this request.
        context: Authenticated identity and current annotation ownership scope.

    Returns:
        The requested saved annotation version.
    """
    return AnnotationVersionResource.from_service(
        service.get_version(annotation_id, version, context=context)
    )
