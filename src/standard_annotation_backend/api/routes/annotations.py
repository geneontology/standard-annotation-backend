"""Expose HTTP operations for creating and managing current annotations."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status

from standard_annotation_backend.api.dependencies import (
    IF_MATCH_OPENAPI,
    IfMatchRequiredError,
    MalformedIfMatchError,
    get_annotation_service,
    get_authenticated_context,
    require_expected_version,
)
from standard_annotation_backend.api.errors import (
    BEARER_ERRORS,
    RequestValidationFailedError,
    error_responses,
)
from standard_annotation_backend.api.examples import (
    ANNOTATION_CREATE_EXAMPLES,
    ANNOTATION_PATCH_EXAMPLES,
)
from standard_annotation_backend.api.models import (
    AnnotationCreateRequest,
    AnnotationListQuery,
    AnnotationPageResponse,
    AnnotationPatchRequest,
    AnnotationResource,
)
from standard_annotation_backend.domain.annotation_search import (
    ClosureTermRequiredError,
    OntologyUnavailableError,
    UnknownQueryParameterError,
    UnsupportedClosureFieldError,
    UnsupportedClosurePredicateError,
    UnsupportedFilterError,
)
from standard_annotation_backend.domain.annotations import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
    DuplicateAnnotationError,
    EmptyAnnotationPatchError,
    InvalidAnnotationPayloadError,
    StaleAnnotationVersionError,
)
from standard_annotation_backend.domain.auth import RequestContext
from standard_annotation_backend.domain.entities import UnknownDbObjectIdError
from standard_annotation_backend.services.annotation_service import AnnotationService

router = APIRouter(
    prefix="/annotations",
    tags=["annotations"],
    dependencies=[Depends(get_authenticated_context)],
    responses=error_responses(*BEARER_ERRORS, RequestValidationFailedError),
)

_ETAG_RESPONSE_HEADER = {
    "description": "Quoted annotation version for subsequent If-Match writes.",
    "schema": {"type": "string", "example": '"1"'},
}

_QUERY_PARAMETER_NAMES = frozenset(AnnotationListQuery.model_fields)
_UNSUPPORTED_FILTER_NAMES = frozenset(
    {"annotation_extensions", "annotation_properties"}
)


def _reject_unknown_query_parameters(request: Request) -> None:
    names = tuple(name for name, _value in request.query_params.multi_items())

    unsupported_closure = next(
        (
            name
            for name in names
            if name.endswith("_closure") and name != "ontology_class_id_closure"
        ),
        None,
    )
    if unsupported_closure is not None:
        raise UnsupportedClosureFieldError(unsupported_closure)

    unsupported_name = next(
        (name for name in names if name in _UNSUPPORTED_FILTER_NAMES),
        None,
    )
    if unsupported_name is not None:
        raise UnsupportedFilterError(unsupported_name)

    unknown_name = next(
        (name for name in names if name not in _QUERY_PARAMETER_NAMES), None
    )
    if unknown_name is not None:
        raise UnknownQueryParameterError(unknown_name)


@router.get(
    "",
    response_model=AnnotationPageResponse,
    dependencies=[Depends(_reject_unknown_query_parameters)],
    # Route-level entries replace the router's entries for the same status, so
    # the bearer and request-validation errors are listed again here.
    responses=error_responses(
        *BEARER_ERRORS,
        UnknownQueryParameterError,
        UnsupportedFilterError,
        ClosureTermRequiredError,
        UnsupportedClosureFieldError,
        UnsupportedClosurePredicateError,
        OntologyUnavailableError,
        RequestValidationFailedError,
    ),
)
def list_annotations(
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
    query: Annotated[AnnotationListQuery, Query()],
) -> AnnotationPageResponse:
    """Return active annotations that match every supplied filter.

    Repeating a list-valued query parameter requires every supplied value to be
    present. Results have a stable order so clients can paginate with an offset.

    Args:
        service: Annotation operations for this request.
        context: Authenticated identity and ownership scope for this query.
        query: Search filters and pagination parameters.

    Returns:
        The requested annotations and pagination information.
    """
    return AnnotationPageResponse.from_service(
        service.list(
            context=context,
            criteria=query.to_filter(),
            limit=query.limit,
            offset=query.offset,
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
        **error_responses(
            DuplicateAnnotationError,
            InvalidAnnotationPayloadError,
            UnknownDbObjectIdError,
            RequestValidationFailedError,
        ),
    },
)
def create_annotation(
    request: Annotated[
        AnnotationCreateRequest,
        Body(openapi_examples=ANNOTATION_CREATE_EXAMPLES),
    ],
    response: Response,
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
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
        **error_responses(AnnotationNotFoundError),
    },
)
def get_annotation(
    annotation_id: UUID,
    response: Response,
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
) -> AnnotationResource:
    """Return the latest state of an active annotation.

    Args:
        annotation_id: Identifier of the annotation to retrieve.
        response: HTTP response whose `ETag` header is set to the current version.
        service: Annotation operations for this request.
        context: Authenticated identity and ownership scope for this read.

    Returns:
        The annotation's current active state.
    """
    result = service.get(annotation_id, context=context)
    response.headers["ETag"] = f'"{result.version}"'
    return AnnotationResource.from_service(result)


@router.patch(
    "/{annotation_id}",
    response_model=AnnotationResource,
    openapi_extra=IF_MATCH_OPENAPI,
    responses={
        status.HTTP_200_OK: {"headers": {"ETag": _ETAG_RESPONSE_HEADER}},
        **error_responses(
            AnnotationNotFoundError,
            AnnotationDeletedError,
            DuplicateAnnotationError,
            StaleAnnotationVersionError,
            IfMatchRequiredError,
            MalformedIfMatchError,
            EmptyAnnotationPatchError,
            InvalidAnnotationPayloadError,
            UnknownDbObjectIdError,
            RequestValidationFailedError,
        ),
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
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
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
    responses=error_responses(
        AnnotationNotFoundError,
        AnnotationDeletedError,
        StaleAnnotationVersionError,
        IfMatchRequiredError,
        MalformedIfMatchError,
        RequestValidationFailedError,
    ),
)
def delete_annotation(
    annotation_id: UUID,
    expected_version: Annotated[int, Depends(require_expected_version)],
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
    context: Annotated[RequestContext, Depends(get_authenticated_context)],
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
