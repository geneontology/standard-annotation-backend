"""Expose global-admin operations: reference data refreshes and job status."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, status

from standard_annotation_backend.api.dependencies import (
    get_authenticated_context,
    get_job_service,
    get_unit_of_work_factory,
)
from standard_annotation_backend.api.errors import BEARER_ERROR_RESPONSES
from standard_annotation_backend.api.examples import (
    ANNOTATION_REFRESH_EXAMPLES,
    ENTITY_REFRESH_EXAMPLES,
    ONTOLOGY_REFRESH_EXAMPLES,
)
from standard_annotation_backend.api.models import (
    AnnotationCutoverRequest,
    ApiErrorResponse,
    JobResource,
    RefreshJobsResource,
    RefreshRequest,
)
from standard_annotation_backend.config import get_settings
from standard_annotation_backend.domain.auth import (
    PermissionAction,
    RequestContext,
    authorize_role,
)
from standard_annotation_backend.domain.refresh import RefreshKindName
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.job_service import Job, JobService
from standard_annotation_backend.services.refresh_start_service import (
    RefreshStartService,
)
from standard_annotation_backend.workers import tasks

router = APIRouter(
    prefix="/admin",
    tags=["admin operations"],
    dependencies=[Depends(get_authenticated_context)],
    responses={**BEARER_ERROR_RESPONSES},
)

_SOURCE_KEY_ERRORS: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ApiErrorResponse,
        "description": (
            "Request validation failed, or source_key is not configured for this "
            "kind (unknown_source)."
        ),
    },
}
_GROUP_SAB_MANAGED_ERROR: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: {
        "model": ApiErrorResponse,
        "description": (
            "The source's group is SAB-managed, so GPAD can no longer replace its "
            "annotations (group_sab_managed). No job is created."
        ),
    },
}
_SHARED_DESCRIPTION = (
    "Queued or running jobs for the same source are returned instead of "
    "duplicated. Requires the admin role with global scope."
)
_RESPONSE_DESCRIPTION = "The created or reused refresh jobs"


def get_refresh_start_service(
    unit_of_work_factory: Annotated[
        UnitOfWorkFactory, Depends(get_unit_of_work_factory)
    ],
) -> RefreshStartService:
    """Create the service that starts refresh jobs for one request."""
    return RefreshStartService(
        unit_of_work_factory,
        get_settings().sources,
        lambda job: tasks.dispatch_refresh_job(job),
    )


def _start(
    service: RefreshStartService,
    context: RequestContext,
    kind: RefreshKindName,
    source_key: str | None,
) -> RefreshJobsResource:
    """Authorize the caller, start the refresh, and build the response."""
    # Check authorization before the source lookup, so callers without access
    # learn nothing about which sources are configured.
    authorize_role(context, PermissionAction.REFRESH_CREATE)
    jobs: tuple[Job, ...] = service.start(
        kind, requested_by=context.actor_id, source_key=source_key
    )
    return RefreshJobsResource(jobs=[JobResource.from_service(job) for job in jobs])


@router.post(
    "/authorization-refreshes",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RefreshJobsResource,
    summary="Refresh authorization",
    description=(
        "Refresh users and authorizations from the configured go-site users.yaml "
        f"source. There is one authorization source, so no body is needed. "
        f"{_SHARED_DESCRIPTION}"
    ),
    response_description=_RESPONSE_DESCRIPTION,
)
def refresh_authorization(
    service: Annotated[RefreshStartService, Depends(get_refresh_start_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> RefreshJobsResource:
    """Start a refresh of the single authorization source."""
    return _start(service, context, RefreshKindName.AUTHORIZATION, None)


@router.post(
    "/ontology-refreshes",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RefreshJobsResource,
    responses=_SOURCE_KEY_ERRORS,
    summary="Refresh ontologies",
    description=(
        "Refresh configured ontologies from their OBO sources, activating a new "
        "snapshot and applying safe term replacements. Send {} to refresh every "
        'configured ontology or {"source_key": "<key>"} for one. '
        f"{_SHARED_DESCRIPTION}"
    ),
    response_description=_RESPONSE_DESCRIPTION,
)
def refresh_ontologies(
    request: Annotated[
        RefreshRequest, Body(openapi_examples=ONTOLOGY_REFRESH_EXAMPLES)
    ],
    service: Annotated[RefreshStartService, Depends(get_refresh_start_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> RefreshJobsResource:
    """Start refreshes of one or every configured ontology source."""
    return _start(service, context, RefreshKindName.ONTOLOGY, request.source_key)


@router.post(
    "/entity-refreshes",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RefreshJobsResource,
    responses=_SOURCE_KEY_ERRORS,
    summary="Refresh entity catalogs",
    description=(
        "Refresh configured entity catalogs from their GPI sources. Send {} to "
        "refresh every configured source and retire catalogs of sources removed "
        'from the configuration, or {"source_key": "<key>"} for one. '
        f"{_SHARED_DESCRIPTION}"
    ),
    response_description=_RESPONSE_DESCRIPTION,
)
def refresh_entities(
    request: Annotated[RefreshRequest, Body(openapi_examples=ENTITY_REFRESH_EXAMPLES)],
    service: Annotated[RefreshStartService, Depends(get_refresh_start_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> RefreshJobsResource:
    """Start refreshes of one or every configured entity source."""
    return _start(service, context, RefreshKindName.ENTITY, request.source_key)


@router.post(
    "/annotation-refreshes",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RefreshJobsResource,
    responses={**_SOURCE_KEY_ERRORS, **_GROUP_SAB_MANAGED_ERROR},
    summary="Replace groups' annotations from GPAD",
    description=(
        "Replace a group's annotations with the contents of its configured GPAD "
        "file. Every successful refresh deletes all of the group's annotations, "
        "including edits made in SAB, and imports the file; a file that is "
        "unchanged while the group is untouched is skipped. Send {} to refresh "
        'every group that GPAD still manages, or {"source_key": "<key>"} for one. '
        f"{_SHARED_DESCRIPTION}"
    ),
    response_description=_RESPONSE_DESCRIPTION,
)
def refresh_annotations(
    request: Annotated[
        RefreshRequest, Body(openapi_examples=ANNOTATION_REFRESH_EXAMPLES)
    ],
    service: Annotated[RefreshStartService, Depends(get_refresh_start_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> RefreshJobsResource:
    """Start GPAD refreshes of one or every GPAD-managed group."""
    return _start(service, context, RefreshKindName.ANNOTATION, request.source_key)


@router.post(
    "/annotation-cutovers",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RefreshJobsResource,
    responses={**_SOURCE_KEY_ERRORS, **_GROUP_SAB_MANAGED_ERROR},
    summary="Move a group to SAB management",
    description=(
        "Run the final GPAD import for one annotation source. It publishes only if "
        "every record is imported, and then moves the source's group to SAB "
        "management permanently: GPAD can never replace that group's annotations "
        "again. A queued or running cutover for the same source is returned "
        "instead of duplicated. Requires the admin role with global scope."
    ),
    response_description="The created or reused cutover job",
)
def cut_over_annotations(
    request: AnnotationCutoverRequest,
    service: Annotated[RefreshStartService, Depends(get_refresh_start_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> RefreshJobsResource:
    """Start the cutover job for one annotation source."""
    # Authorize before the source lookup, as `_start` does.
    authorize_role(context, PermissionAction.REFRESH_CREATE)
    job = service.start_cutover(
        requested_by=context.actor_id, source_key=request.source_key
    )
    return RefreshJobsResource(jobs=[JobResource.from_service(job)])


@router.get(
    "/jobs/{job_id}",
    response_model=JobResource,
    responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
    summary="Read a job",
    description=(
        "Return a job's status, progress, warnings, and result. Requires the "
        "admin role with global scope."
    ),
    response_description="The job's current state",
)
def get_job(
    job_id: UUID,
    service: Annotated[JobService, Depends(get_job_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> JobResource:
    """Return job status to an administrator with global scope."""
    return JobResource.from_service(service.get(context, job_id))
