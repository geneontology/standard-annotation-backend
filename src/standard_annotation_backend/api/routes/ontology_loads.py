"""Create stored ontology-load jobs for worker execution."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status

from standard_annotation_backend.api.dependencies import (
    get_authenticated_context,
    get_job_service,
)
from standard_annotation_backend.api.errors import BEARER_ERROR_RESPONSES
from standard_annotation_backend.api.models import (
    ApiErrorResponse,
    JobResource,
    OntologyLoadRequest,
)
from standard_annotation_backend.domain.auth import (
    PermissionAction,
    RequestContext,
    authorize_role,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.services.job_service import JobService
from standard_annotation_backend.workers import tasks

logger = logging.getLogger(__name__)

_DISPATCH_FAILURE = "Ontology load could not be dispatched"

router = APIRouter(
    prefix="/ontology-loads",
    tags=["ontology loads"],
    dependencies=[Depends(get_authenticated_context)],
    responses={
        **BEARER_ERROR_RESPONSES,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ApiErrorResponse},
    },
)


@router.post(
    "",
    response_model=JobResource,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_ontology_load(
    request: OntologyLoadRequest,
    service: Annotated[JobService, Depends(get_job_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> JobResource:
    """Create an ontology-load job and send it to a worker."""
    authorize_role(context, PermissionAction.ONTOLOGY_LOAD_CREATE)
    job = service.create(
        job_type=JobType.ONTOLOGY_LOAD,
        requested_by=context.actor_id,
        parameters={"ontology": request.ontology.value},
    )
    try:
        tasks.run_ontology_load.delay(str(job.job_id))
    except Exception as error:
        logger.error(
            "Ontology load dispatch failed: job_id=%s failure_type=%s",
            job.job_id,
            type(error).__name__,
        )
        job = service.fail(job.job_id, error=_DISPATCH_FAILURE)
    return JobResource.from_service(job)
