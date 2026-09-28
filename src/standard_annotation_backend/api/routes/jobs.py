"""Provide authenticated API access to asynchronous job status."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from standard_annotation_backend.api.dependencies import (
    get_authenticated_context,
    get_job_service,
)
from standard_annotation_backend.api.errors import BEARER_ERROR_RESPONSES
from standard_annotation_backend.api.models import ApiErrorResponse, JobResource
from standard_annotation_backend.domain.auth import RequestContext
from standard_annotation_backend.services.job_service import JobService

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(get_authenticated_context)],
    responses={
        **BEARER_ERROR_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
    },
)


@router.get("/{job_id}", response_model=JobResource)
def get_job(
    job_id: UUID,
    service: Annotated[JobService, Depends(get_job_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> JobResource:
    """Return job status to an administrator with global scope."""
    return JobResource.from_service(service.get(context, job_id))
