"""Expose annotation QC reports: start them, and read summaries and findings."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, status

from standard_annotation_backend.api.dependencies import (
    get_authenticated_context,
    get_unit_of_work_factory,
)
from standard_annotation_backend.api.errors import (
    BEARER_ERRORS,
    RequestValidationFailedError,
    error_responses,
)
from standard_annotation_backend.api.examples import QC_REPORT_EXAMPLES
from standard_annotation_backend.api.models import (
    JobResource,
    QcFindingPageResponse,
    QcFindingQuery,
    QcReportRequest,
    QcReportResource,
)
from standard_annotation_backend.domain.annotation_search import (
    UnknownQueryParameterError,
)
from standard_annotation_backend.domain.auth import RequestContext
from standard_annotation_backend.domain.qc_reports import (
    QcReportFindingsExpiredError,
    QcReportNotCompletedError,
    QcReportNotFoundError,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.qc_report_service import QcReportService
from standard_annotation_backend.workers import tasks

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(get_authenticated_context)],
    responses=error_responses(*BEARER_ERRORS),
)

_FINDING_QUERY_PARAMETERS = frozenset(QcFindingQuery.model_fields)


def get_qc_report_service(
    unit_of_work_factory: Annotated[
        UnitOfWorkFactory, Depends(get_unit_of_work_factory)
    ],
) -> QcReportService:
    """Create the QC report service for one request."""
    return QcReportService(
        unit_of_work_factory, lambda job: tasks.dispatch_qc_report(job)
    )


def _reject_unknown_query_parameters(request: Request) -> None:
    """Reject findings query parameters the route does not define."""
    unknown_name = next(
        (
            name
            for name, _value in request.query_params.multi_items()
            if name not in _FINDING_QUERY_PARAMETERS
        ),
        None,
    )
    if unknown_name is not None:
        raise UnknownQueryParameterError(unknown_name)


@router.post(
    "/annotation-qc",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobResource,
    responses=error_responses(RequestValidationFailedError),
    summary="Start an annotation QC report",
    description=(
        "Validate every active annotation in scope with the same checks as "
        "writes, and keep one finding for each annotation with issues. Requires "
        "the admin role: a global admin reports on one group or, with {}, on all "
        "groups, and a group admin reports on its own group. A queued report of "
        "the same scope is returned instead of duplicated."
    ),
    response_description="The created or reused report job",
)
def start_qc_report(
    request: Annotated[QcReportRequest, Body(openapi_examples=QC_REPORT_EXAMPLES)],
    service: Annotated[QcReportService, Depends(get_qc_report_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> JobResource:
    """Queue a QC report, or join the queued one of the same scope."""
    return JobResource.from_service(service.start(context, request.owning_group_id))


@router.get(
    "/annotation-qc/{report_id}",
    response_model=QcReportResource,
    responses=error_responses(QcReportNotFoundError, RequestValidationFailedError),
    summary="Read a QC report",
    description=(
        "Return a report's status, scope, timing, the reference data versions "
        "it used, and its counts by issue type and by group. Counts cover the "
        "caller's group, or every group for global scope. Requires the read role."
    ),
    response_description="The report's summary",
)
def read_qc_report(
    report_id: UUID,
    service: Annotated[QcReportService, Depends(get_qc_report_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> QcReportResource:
    """Return a report's summary within the caller's groups."""
    return QcReportResource.from_service(service.summary(context, report_id))


@router.get(
    "/annotation-qc/{report_id}/findings",
    response_model=QcFindingPageResponse,
    dependencies=[Depends(_reject_unknown_query_parameters)],
    responses=error_responses(
        QcReportNotFoundError,
        QcReportNotCompletedError,
        QcReportFindingsExpiredError,
        UnknownQueryParameterError,
        RequestValidationFailedError,
    ),
    summary="List a QC report's findings",
    description=(
        "List the findings of a succeeded report, in annotation ID order, within "
        "the caller's ownership scope as in annotation search. Findings are kept "
        "only for the most recent completed report of each scope. Requires the "
        "read role."
    ),
    response_description="One page of findings",
)
def list_qc_findings(
    report_id: UUID,
    service: Annotated[QcReportService, Depends(get_qc_report_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
    query: Annotated[QcFindingQuery, Query()],
) -> QcFindingPageResponse:
    """Return one page of findings within the caller's ownership scope."""
    return QcFindingPageResponse.from_service(
        service.findings(
            context,
            report_id,
            issue_type=query.issue_type,
            owning_group_id=query.owning_group_id,
            limit=query.limit,
            offset=query.offset,
        )
    )
