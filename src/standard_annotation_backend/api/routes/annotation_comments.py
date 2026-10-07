"""Expose HTTP operations for annotation comments."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Response, status

from standard_annotation_backend.api.dependencies import (
    PageLimit,
    PageOffset,
    get_authenticated_context,
    get_comment_service,
)
from standard_annotation_backend.api.errors import (
    BEARER_ERROR_RESPONSES,
    RequestValidationFailedError,
    error_responses,
)
from standard_annotation_backend.api.examples import (
    ANNOTATION_COMMENT_CREATE_EXAMPLES,
    ANNOTATION_COMMENT_EDIT_EXAMPLES,
)
from standard_annotation_backend.api.models import (
    AnnotationCommentPageResponse,
    AnnotationCommentRequest,
    AnnotationCommentResource,
)
from standard_annotation_backend.domain.annotations import AnnotationNotFoundError
from standard_annotation_backend.domain.auth import RequestContext
from standard_annotation_backend.domain.comments import (
    CommentNotFoundError,
    InvalidCommentError,
)
from standard_annotation_backend.services.comment_service import CommentService

router = APIRouter(
    prefix="/annotations/{annotation_id}/comments",
    tags=["annotation comments"],
    dependencies=[Depends(get_authenticated_context)],
    responses={
        **BEARER_ERROR_RESPONSES,
        **error_responses(
            AnnotationNotFoundError,
            CommentNotFoundError,
            InvalidCommentError,
            RequestValidationFailedError,
        ),
    },
)


@router.get("", response_model=AnnotationCommentPageResponse)
def list_annotation_comments(
    annotation_id: UUID,
    service: Annotated[CommentService, Depends(get_comment_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
    limit: PageLimit = 50,
    offset: PageOffset = 0,
) -> AnnotationCommentPageResponse:
    """Return visible comments for an accessible annotation.

    Args:
        annotation_id: Identifier of the annotation whose comments are requested.
        service: Comment operations for this request.
        context: Authenticated identity and ownership scope for this read.
        limit: Maximum number of comments to return.
        offset: Number of visible comments to skip.

    Returns:
        The requested comments and pagination information.
    """
    return AnnotationCommentPageResponse.from_service(
        service.list(
            annotation_id,
            context=context,
            limit=limit,
            offset=offset,
        )
    )


@router.post(
    "",
    response_model=AnnotationCommentResource,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_201_CREATED: {
            "headers": {
                "Location": {
                    "description": "Path to the created annotation comment.",
                    "schema": {"type": "string"},
                }
            }
        }
    },
)
def create_annotation_comment(
    annotation_id: UUID,
    request: Annotated[
        AnnotationCommentRequest,
        Body(openapi_examples=ANNOTATION_COMMENT_CREATE_EXAMPLES),
    ],
    response: Response,
    service: Annotated[CommentService, Depends(get_comment_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> AnnotationCommentResource:
    """Create a comment against the annotation's current version.

    Args:
        annotation_id: Identifier of the annotation being discussed.
        request: Comment text supplied by the client.
        response: HTTP response whose `Location` header is set.
        service: Comment operations for this request.
        context: Authenticated identity recorded as the comment author.

    Returns:
        The created version-pinned comment.
    """
    result = service.create(annotation_id, body=request.body, context=context)
    response.headers["Location"] = (
        f"/annotations/{annotation_id}/comments/{result.comment_id}"
    )
    return AnnotationCommentResource.from_service(result)


@router.patch("/{comment_id}", response_model=AnnotationCommentResource)
def edit_annotation_comment(
    annotation_id: UUID,
    comment_id: UUID,
    request: Annotated[
        AnnotationCommentRequest,
        Body(openapi_examples=ANNOTATION_COMMENT_EDIT_EXAMPLES),
    ],
    service: Annotated[CommentService, Depends(get_comment_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> AnnotationCommentResource:
    """Edit an annotation comment as its author or an administrator.

    Args:
        annotation_id: Identifier of the parent annotation.
        comment_id: Identifier of the comment to edit.
        request: Replacement comment text supplied by the client.
        service: Comment operations for this request.
        context: Authenticated identity and selected authorization.

    Returns:
        The updated comment.
    """
    return AnnotationCommentResource.from_service(
        service.edit(
            annotation_id,
            comment_id,
            body=request.body,
            context=context,
        )
    )


@router.delete("/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_annotation_comment(
    annotation_id: UUID,
    comment_id: UUID,
    service: Annotated[CommentService, Depends(get_comment_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> Response:
    """Soft-delete an annotation comment as its author or an administrator.

    Args:
        annotation_id: Identifier of the parent annotation.
        comment_id: Identifier of the comment to delete.
        service: Comment operations for this request.
        context: Authenticated identity and selected authorization.

    Returns:
        An empty HTTP 204 response after the deletion is saved.
    """
    service.delete(annotation_id, comment_id, context=context)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
