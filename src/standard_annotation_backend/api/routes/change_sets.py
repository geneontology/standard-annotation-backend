"""Expose HTTP operations for proposing and reviewing annotation changes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from standard_annotation_backend.api.dependencies import (
    get_change_set_service,
    get_request_context,
)
from standard_annotation_backend.api.models import (
    AcceptedChangeSetResource,
    ApiErrorResponse,
    ChangeSetAcceptRequest,
    ChangeSetCreateRequest,
    ChangeSetPreviewResource,
    ChangeSetProposalRequest,
    ChangeSetRejectRequest,
    ChangeSetResource,
    ChangeSetUpdateRequest,
)
from standard_annotation_backend.services.annotation_service import RequestContext
from standard_annotation_backend.services.change_set_service import ChangeSetService

router = APIRouter(
    prefix="/change-sets",
    tags=["change-sets"],
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ApiErrorResponse},
    },
)


@router.post(
    "",
    response_model=ChangeSetResource,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_201_CREATED: {
            "headers": {
                "Location": {
                    "description": "Path to the proposed change set.",
                    "schema": {"type": "string"},
                }
            }
        }
    },
)
def propose_change_set(
    body: ChangeSetProposalRequest,
    response: Response,
    service: Annotated[ChangeSetService, Depends(get_change_set_service)],
    context: Annotated[RequestContext, Depends(get_request_context)],
) -> ChangeSetResource:
    """Store a proposal without changing an annotation.

    Create proposals carry raw annotation data for validation during review.
    Update and delete proposals identify a saved base version.
    """
    if isinstance(body, ChangeSetCreateRequest):
        result = service.propose_create(
            payload=body.annotation,
            owning_group_id=body.owning_group_id,
            reason=body.reason,
            context=context,
        )
    elif isinstance(body, ChangeSetUpdateRequest):
        result = service.propose_update(
            body.annotation_id,
            base_version=body.base_version,
            patch=body.patch,
            reason=body.reason,
            context=context,
        )
    else:
        result = service.propose_delete(
            body.annotation_id,
            base_version=body.base_version,
            reason=body.reason,
            context=context,
        )
    response.headers["Location"] = f"/change-sets/{result.change_set_id}"
    return ChangeSetResource.from_service(result)


@router.get("/{change_set_id}", response_model=ChangeSetResource)
def get_change_set(
    change_set_id: UUID,
    service: Annotated[ChangeSetService, Depends(get_change_set_service)],
) -> ChangeSetResource:
    """Read a proposal, its stored preview, and any completed review."""
    return ChangeSetResource.from_service(service.get(change_set_id))


@router.post(
    "/{change_set_id}/preview",
    response_model=ChangeSetPreviewResource,
    responses={status.HTTP_409_CONFLICT: {"model": ApiErrorResponse}},
)
def preview_change_set(
    change_set_id: UUID,
    service: Annotated[ChangeSetService, Depends(get_change_set_service)],
) -> ChangeSetPreviewResource:
    """Validate and store an advisory preview without accepting the proposal."""
    return ChangeSetPreviewResource.from_service(service.preview(change_set_id))


@router.post(
    "/{change_set_id}/accept",
    response_model=AcceptedChangeSetResource,
    responses={status.HTTP_409_CONFLICT: {"model": ApiErrorResponse}},
)
def accept_change_set(
    change_set_id: UUID,
    service: Annotated[ChangeSetService, Depends(get_change_set_service)],
    context: Annotated[RequestContext, Depends(get_request_context)],
    body: ChangeSetAcceptRequest | None = None,
) -> AcceptedChangeSetResource:
    """Accept an eligible proposal and return the resulting annotation version.

    Deletions also return a resource, with `is_deleted` set to true.
    """
    return AcceptedChangeSetResource.from_service(
        service.accept(
            change_set_id,
            context=context,
            review_reason=body.review_reason if body is not None else None,
        )
    )


@router.post(
    "/{change_set_id}/reject",
    response_model=ChangeSetResource,
    responses={status.HTTP_409_CONFLICT: {"model": ApiErrorResponse}},
)
def reject_change_set(
    change_set_id: UUID,
    body: ChangeSetRejectRequest,
    service: Annotated[ChangeSetService, Depends(get_change_set_service)],
    context: Annotated[RequestContext, Depends(get_request_context)],
) -> ChangeSetResource:
    """Reject a proposal with a required explanation from the reviewer."""
    return ChangeSetResource.from_service(
        service.reject(
            change_set_id,
            review_reason=body.review_reason,
            context=context,
        )
    )
