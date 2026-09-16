"""Expose HTTP operations for creating and managing current annotations."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status

from standard_annotation_backend.api.dependencies import (
    IF_MATCH_OPENAPI,
    get_annotation_service,
    get_request_context,
    require_expected_version,
)
from standard_annotation_backend.api.errors import ApiError
from standard_annotation_backend.api.examples import (
    ANNOTATION_CREATE_EXAMPLES,
    ANNOTATION_PATCH_EXAMPLES,
)
from standard_annotation_backend.api.models import (
    AnnotationCreateRequest,
    AnnotationPageResponse,
    AnnotationPatchRequest,
    AnnotationResource,
    ApiErrorResponse,
)
from standard_annotation_backend.services.annotation_service import (
    AnnotationService,
    RequestContext,
)

router = APIRouter(
    prefix="/annotations",
    tags=["annotations"],
    responses={status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ApiErrorResponse}},
)

_ETAG_RESPONSE_HEADER = {
    "description": "Quoted annotation version for subsequent If-Match writes.",
    "schema": {"type": "string", "example": '"1"'},
}

_FILTER_NAMES = frozenset(
    {
        "db_object_id",
        "negation",
        "relation",
        "ontology_class_id",
        "references",
        "evidence_type",
        "with_or_from",
        "interacting_taxon_id",
        "annotation_date",
        "assigned_by",
    }
)
_PAGINATION_NAMES = frozenset({"limit", "offset"})
_UNSUPPORTED_FILTER_NAMES = frozenset(
    {"annotation_extensions", "annotation_properties"}
)


def _reject_unknown_query_parameters(request: Request) -> None:
    allowed_names = _FILTER_NAMES | _PAGINATION_NAMES
    names = tuple(name for name, _value in request.query_params.multi_items())

    unsupported_name = next(
        (name for name in names if name in _UNSUPPORTED_FILTER_NAMES),
        None,
    )
    if unsupported_name is not None:
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="unsupported_filter",
            message=f"Unsupported annotation filter: {unsupported_name}",
        )

    unknown_name = next((name for name in names if name not in allowed_names), None)
    if unknown_name is not None:
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="unknown_query_parameter",
            message=f"Unknown query parameter: {unknown_name}",
        )


@router.get(
    "",
    response_model=AnnotationPageResponse,
    dependencies=[Depends(_reject_unknown_query_parameters)],
    responses={status.HTTP_400_BAD_REQUEST: {"model": ApiErrorResponse}},
)
def list_annotations(
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
    db_object_id: Annotated[str | None, Query()] = None,
    negation: Annotated[bool | None, Query()] = None,
    relation: Annotated[str | None, Query()] = None,
    ontology_class_id: Annotated[str | None, Query()] = None,
    references: Annotated[list[str] | None, Query()] = None,
    evidence_type: Annotated[str | None, Query()] = None,
    with_or_from: Annotated[list[str] | None, Query()] = None,
    interacting_taxon_id: Annotated[list[str] | None, Query()] = None,
    annotation_date: Annotated[date | None, Query()] = None,
    assigned_by: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AnnotationPageResponse:
    """Return active annotations that match every supplied filter.

    Repeating a list-valued query parameter requires every supplied value to be
    present. Results have a stable order so clients can paginate with an offset.

    Args:
        service: Annotation operations for this request.
        db_object_id: Database object identifier to match.
        negation: Negation value to match.
        relation: Relation identifier to match.
        ontology_class_id: Ontology class identifier to match.
        references: Reference identifiers that must all be present.
        evidence_type: Evidence type identifier to match.
        with_or_from: Supporting identifiers that must all be present.
        interacting_taxon_id: Taxon identifiers that must all be present.
        annotation_date: Annotation date to match.
        assigned_by: Assigning organization to match.
        limit: Maximum number of annotations to return.
        offset: Number of matching annotations to skip.

    Returns:
        The requested annotations and pagination information.
    """
    return AnnotationPageResponse.from_service(
        service.list(
            db_object_id=db_object_id,
            negation=negation,
            relation=relation,
            ontology_class_id=ontology_class_id,
            references=tuple(references or ()),
            evidence_type=evidence_type,
            with_or_from=tuple(with_or_from or ()),
            interacting_taxon_id=tuple(interacting_taxon_id or ()),
            annotation_date=annotation_date,
            assigned_by=assigned_by,
            limit=limit,
            offset=offset,
        )
    )


@router.post(
    "",
    response_model=AnnotationResource,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_201_CREATED: {
            "headers": {
                "ETag": _ETAG_RESPONSE_HEADER,
                "Location": {
                    "description": "Path to the created annotation.",
                    "schema": {"type": "string"},
                },
            }
        },
        status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
    },
)
def create_annotation(
    request: Annotated[
        AnnotationCreateRequest,
        Body(openapi_examples=ANNOTATION_CREATE_EXAMPLES),
    ],
    response: Response,
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
    context: Annotated[RequestContext, Depends(get_request_context)],
) -> AnnotationResource:
    """Create an annotation from data submitted directly through the API.

    Args:
        request: Owning group and annotation data supplied by the client.
        response: HTTP response whose `Location` and `ETag` headers are set.
        service: Annotation operations for this request.
        context: Identity information recorded in version history.

    Returns:
        The created annotation at version 1.
    """
    result = service.create(
        payload=request.annotation.model_dump(mode="json"),
        owning_group_id=request.owning_group_id,
        context=context,
    )
    response.headers["Location"] = f"/annotations/{result.annotation_id}"
    response.headers["ETag"] = f'"{result.version}"'
    return AnnotationResource.from_service(result)


@router.get(
    "/{annotation_id}",
    response_model=AnnotationResource,
    responses={
        status.HTTP_200_OK: {"headers": {"ETag": _ETAG_RESPONSE_HEADER}},
        status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
    },
)
def get_annotation(
    annotation_id: UUID,
    response: Response,
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
) -> AnnotationResource:
    """Return the latest state of an active annotation.

    Args:
        annotation_id: Identifier of the annotation to retrieve.
        response: HTTP response whose `ETag` header is set to the current version.
        service: Annotation operations for this request.

    Returns:
        The annotation's current active state.
    """
    result = service.get(annotation_id)
    response.headers["ETag"] = f'"{result.version}"'
    return AnnotationResource.from_service(result)


@router.patch(
    "/{annotation_id}",
    response_model=AnnotationResource,
    openapi_extra=IF_MATCH_OPENAPI,
    responses={
        status.HTTP_200_OK: {"headers": {"ETag": _ETAG_RESPONSE_HEADER}},
        status.HTTP_400_BAD_REQUEST: {"model": ApiErrorResponse},
        status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        status.HTTP_412_PRECONDITION_FAILED: {"model": ApiErrorResponse},
        status.HTTP_428_PRECONDITION_REQUIRED: {"model": ApiErrorResponse},
    },
)
def patch_annotation(
    annotation_id: UUID,
    patch: Annotated[
        AnnotationPatchRequest,
        Body(openapi_examples=ANNOTATION_PATCH_EXAMPLES),
    ],
    response: Response,
    expected_version: Annotated[int, Depends(require_expected_version)],
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
    context: Annotated[RequestContext, Depends(get_request_context)],
) -> AnnotationResource:
    """Replace selected fields and save a new annotation version.

    Args:
        annotation_id: Identifier of the annotation to update.
        patch: Fields supplied by the client and their replacement values.
        response: HTTP response whose `ETag` header is set to the new version.
        expected_version: Version read from `If-Match` that must still be current.
        service: Annotation operations for this request.
        context: Identity information recorded in version history.

    Returns:
        The updated annotation and its new version number.
    """
    result = service.patch(
        annotation_id,
        changes=patch.changes(),
        expected_version=expected_version,
        context=context,
    )
    response.headers["ETag"] = f'"{result.version}"'
    return AnnotationResource.from_service(result)


@router.delete(
    "/{annotation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra=IF_MATCH_OPENAPI,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ApiErrorResponse},
        status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
        status.HTTP_412_PRECONDITION_FAILED: {"model": ApiErrorResponse},
        status.HTTP_428_PRECONDITION_REQUIRED: {"model": ApiErrorResponse},
    },
)
def delete_annotation(
    annotation_id: UUID,
    expected_version: Annotated[int, Depends(require_expected_version)],
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
    context: Annotated[RequestContext, Depends(get_request_context)],
) -> Response:
    """Mark an annotation as deleted while preserving its saved versions.

    Args:
        annotation_id: Identifier of the annotation to delete.
        expected_version: Version read from `If-Match` that must still be current.
        service: Annotation operations for this request.
        context: Identity information recorded in version history.

    Returns:
        An empty HTTP 204 response after the deletion is saved.
    """
    service.delete(
        annotation_id,
        expected_version=expected_version,
        context=context,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
