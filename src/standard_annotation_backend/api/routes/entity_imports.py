"""Start entity import jobs for configured entity sources."""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from standard_annotation_backend.api.dependencies import (
    get_authenticated_context,
    get_entity_source_registry,
    get_unit_of_work_factory,
)
from standard_annotation_backend.api.errors import BEARER_ERROR_RESPONSES
from standard_annotation_backend.api.models import (
    ApiErrorResponse,
    EntityImportJobsResource,
    EntityImportRequest,
    JobResource,
)
from standard_annotation_backend.domain.auth import (
    PermissionAction,
    RequestContext,
    authorize_role,
)
from standard_annotation_backend.entity_sources.registry import EntitySourceRegistry
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.entity_import_run_service import (
    EntityImportRunService,
)
from standard_annotation_backend.workers import tasks

router = APIRouter(
    prefix="/entity-imports",
    tags=["entity imports"],
    dependencies=[Depends(get_authenticated_context)],
    responses={
        **BEARER_ERROR_RESPONSES,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ApiErrorResponse,
            "description": (
                "Request validation failed, or source_key is not configured "
                "(unknown_entity_source)."
            ),
        },
    },
)


def get_entity_import_run_service(
    unit_of_work_factory: Annotated[
        UnitOfWorkFactory, Depends(get_unit_of_work_factory)
    ],
    registry: Annotated[EntitySourceRegistry, Depends(get_entity_source_registry)],
) -> EntityImportRunService:
    """Create the service that starts entity import jobs for one request."""
    return EntityImportRunService(
        unit_of_work_factory, registry, tasks.dispatch_entity_job
    )


@router.post(
    "",
    response_model=EntityImportJobsResource,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create entity imports",
    description=(
        "Import one configured entity source, or omit source_key to import every "
        "configured source and retire catalogs whose source was removed. Existing "
        "queued or running jobs for the same source are returned instead of "
        "duplicated. Requires the admin role with global scope."
    ),
    response_description="The created or reused entity jobs",
)
def create_entity_import(
    request: EntityImportRequest,
    service: Annotated[EntityImportRunService, Depends(get_entity_import_run_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> EntityImportJobsResource:
    """Start entity imports for one or all configured sources."""
    authorize_role(context, PermissionAction.ENTITY_IMPORT_CREATE)
    jobs = service.start(requested_by=context.actor_id, source_key=request.source_key)
    return EntityImportJobsResource(
        jobs=[JobResource.from_service(job) for job in jobs]
    )
