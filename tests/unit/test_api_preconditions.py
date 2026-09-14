"""Test annotation request models, version headers, and HTTP error responses."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import pytest
from fastapi import Body, Depends, FastAPI, Query, status
from fastapi.testclient import TestClient
from pydantic import ValidationError

from standard_annotation_backend.api.dependencies import (
    get_request_context,
    parse_if_match,
    require_expected_version,
)
from standard_annotation_backend.api.errors import ApiError, install_exception_handlers
from standard_annotation_backend.api.models import (
    AnnotationCreateRequest,
    AnnotationPageResponse,
    AnnotationPatchRequest,
    AnnotationResource,
    AnnotationVersionPageResponse,
    AnnotationVersionResource,
)
from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence.repositories import (
    AnnotationDeletedError,
    AnnotationNotFoundError,
    DuplicateAnnotationError,
    StaleAnnotationVersionError,
)
from standard_annotation_backend.services.annotation_service import (
    AnnotationHistoryNotFoundError,
    AnnotationVersion,
    CurrentAnnotation,
    EmptyAnnotationPatchError,
    InvalidAnnotationPayloadError,
    ResultPage,
)

ANNOTATION_ID = UUID("00000000-0000-0000-0000-000000000401")
PEER_A = UUID("00000000-0000-0000-0000-000000000402")
PEER_B = UUID("00000000-0000-0000-0000-000000000403")
CREATED_AT = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
UPDATED_AT = datetime(2026, 9, 11, 12, 5, tzinfo=UTC)


def _valid_annotation_payload() -> dict[str, object]:
    return {
        "db_object_id": "UniProtKB:P12345",
        "relation": "RO:0002331",
        "ontology_class_id": "GO:0008150",
        "references": ["PMID:1"],
        "evidence_type": "ECO:0000314",
        "annotation_date": "2026-09-11",
        "assigned_by": "GO_Central",
    }


@pytest.mark.parametrize(
    ("header", "expected_status", "expected_code", "expected_message"),
    [
        (
            None,
            status.HTTP_428_PRECONDITION_REQUIRED,
            "precondition_required",
            "If-Match is required",
        ),
        (
            "1",
            status.HTTP_400_BAD_REQUEST,
            "malformed_precondition",
            "If-Match must be one quoted positive integer",
        ),
        (
            'W/"1"',
            status.HTTP_400_BAD_REQUEST,
            "malformed_precondition",
            "If-Match must be one quoted positive integer",
        ),
        (
            "*",
            status.HTTP_400_BAD_REQUEST,
            "malformed_precondition",
            "If-Match must be one quoted positive integer",
        ),
        (
            '"1", "2"',
            status.HTTP_400_BAD_REQUEST,
            "malformed_precondition",
            "If-Match must be one quoted positive integer",
        ),
        (
            '"0"',
            status.HTTP_400_BAD_REQUEST,
            "malformed_precondition",
            "If-Match must be one quoted positive integer",
        ),
        (
            '"-1"',
            status.HTTP_400_BAD_REQUEST,
            "malformed_precondition",
            "If-Match must be one quoted positive integer",
        ),
        (
            '"abc"',
            status.HTTP_400_BAD_REQUEST,
            "malformed_precondition",
            "If-Match must be one quoted positive integer",
        ),
    ],
)
def test_if_match_rejects_missing_or_malformed_values(
    header: str | None,
    expected_status: int,
    expected_code: str,
    expected_message: str,
) -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.patch("/resource")
    def route(
        expected_version: Annotated[int, Depends(require_expected_version)],
    ) -> dict[str, int]:
        return {"expected_version": expected_version}

    headers = {} if header is None else {"If-Match": header}
    response = TestClient(app).patch("/resource", headers=headers)

    assert response.status_code == expected_status
    assert response.json() == {
        "error": {"code": expected_code, "message": expected_message}
    }


def test_if_match_returns_the_quoted_positive_version() -> None:
    assert parse_if_match('"27"') == 27


def test_if_match_rejects_an_integer_exceeding_the_runtime_conversion_limit() -> None:
    with pytest.raises(ApiError) as raised:
        parse_if_match('"' + "9" * 4301 + '"')

    assert raised.value.status_code == status.HTTP_400_BAD_REQUEST
    assert raised.value.code == "malformed_precondition"


def test_create_request_model_rejects_blank_owning_group_with_stable_error() -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.post("/resource")
    def route(request: Annotated[AnnotationCreateRequest, Body()]) -> None:
        del request

    response = TestClient(app).post(
        "/resource",
        json={"owning_group_id": "   ", "annotation": _valid_annotation_payload()},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    error = response.json()["error"]
    assert error["code"] == "request_validation_error"
    assert error["message"] == "Request validation failed"
    assert len(error["details"]) == 1
    issue = error["details"][0]
    assert issue["location"] == ["body", "owning_group_id"]
    assert issue["type"] == "string_pattern_mismatch"
    assert isinstance(issue["message"], str) and issue["message"]


def test_patch_request_model_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError) as raised:
        AnnotationPatchRequest.model_validate({"unexpected": True})

    issues = raised.value.errors(include_url=False, include_context=False)
    assert len(issues) == 1
    assert issues[0]["type"] == "extra_forbidden"
    assert issues[0]["loc"] == ("unexpected",)
    assert issues[0]["input"] is True
    assert isinstance(issues[0]["msg"], str) and issues[0]["msg"]


def test_empty_patch_model_retains_an_empty_fields_set() -> None:
    patch = AnnotationPatchRequest.model_validate({})

    assert patch.model_fields_set == set()
    assert patch.changes() == {}


def test_patch_model_preserves_explicit_null_in_changes() -> None:
    patch = AnnotationPatchRequest.model_validate({"negation": None})

    assert patch.model_fields_set == {"negation"}
    assert patch.changes() == {"negation": None}


def test_patch_model_represents_every_generated_annotation_field() -> None:
    assert set(AnnotationPatchRequest.model_fields) == set(Annotation.model_fields)


def test_resource_models_convert_all_service_results() -> None:
    annotation = Annotation.model_validate(_valid_annotation_payload())
    current = CurrentAnnotation(
        annotation_id=ANNOTATION_ID,
        version=2,
        owning_group_id="group-1",
        record_origin="direct",
        source_import_job_id=None,
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
        annotation=annotation,
    )
    version = AnnotationVersion(
        annotation_id=ANNOTATION_ID,
        version=1,
        is_deleted=False,
        actor_id="provisional-api-user",
        change_source="api",
        created_at=CREATED_AT,
        annotation=annotation,
    )

    current_resource = AnnotationResource.from_service(current)
    version_resource = AnnotationVersionResource.from_service(version)
    current_page = AnnotationPageResponse.from_service(
        ResultPage(items=(current,), total=1, limit=50, offset=0)
    )
    version_page = AnnotationVersionPageResponse.from_service(
        ResultPage(items=(version,), total=1, limit=50, offset=0)
    )

    assert current_resource.annotation is annotation
    assert current_resource.annotation_id == ANNOTATION_ID
    assert current_resource.version == 2
    assert version_resource.annotation is annotation
    assert version_resource.actor_id == "provisional-api-user"
    assert current_page.items == [current_resource]
    assert current_page.model_dump()["total"] == 1
    assert version_page.items == [version_resource]
    assert version_page.model_dump()["limit"] == 50


def test_default_request_context_model_uses_the_provisional_actor() -> None:
    assert get_request_context().actor_id == "provisional-api-user"


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            AnnotationNotFoundError(ANNOTATION_ID),
            status.HTTP_404_NOT_FOUND,
            "annotation_not_found",
        ),
        (
            AnnotationDeletedError(ANNOTATION_ID),
            status.HTTP_404_NOT_FOUND,
            "annotation_not_found",
        ),
        (
            AnnotationHistoryNotFoundError(ANNOTATION_ID),
            status.HTTP_404_NOT_FOUND,
            "annotation_not_found",
        ),
        (
            AnnotationHistoryNotFoundError(ANNOTATION_ID, 7),
            status.HTTP_404_NOT_FOUND,
            "version_not_found",
        ),
        (
            EmptyAnnotationPatchError(),
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "empty_annotation_patch",
        ),
    ],
)
def test_expected_errors_map_to_stable_statuses_and_codes(
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/resource")
    def route() -> None:
        raise error

    response = TestClient(app).get("/resource")

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


def test_duplicate_annotation_error_maps_peer_ids_to_conflict_details() -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/resource")
    def route() -> None:
        raise DuplicateAnnotationError((PEER_A, PEER_B))

    response = TestClient(app).get("/resource")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json() == {
        "error": {
            "code": "duplicate_annotation",
            "message": "Annotation conflicts with active duplicates",
            "details": {"peer_ids": [str(PEER_A), str(PEER_B)]},
        }
    }


def test_stale_annotation_error_maps_version_details_to_precondition_failed() -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/resource")
    def route() -> None:
        raise StaleAnnotationVersionError(ANNOTATION_ID, 2, 3)

    response = TestClient(app).get("/resource")

    assert response.status_code == status.HTTP_412_PRECONDITION_FAILED
    assert response.json() == {
        "error": {
            "code": "stale_annotation_version",
            "message": "Annotation version does not match If-Match",
            "details": {
                "annotation_id": str(ANNOTATION_ID),
                "expected_version": 2,
                "current_version": 3,
            },
        }
    }


def test_invalid_annotation_error_preserves_normalized_validation_details() -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/resource")
    def route() -> None:
        raise InvalidAnnotationPayloadError(
            (
                {
                    "location": ("db_object_id",),
                    "message": "A supplied domain validation message",
                    "type": "missing",
                },
            )
        )

    response = TestClient(app).get("/resource")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json() == {
        "error": {
            "code": "invalid_annotation",
            "message": "Annotation payload is invalid",
            "details": [
                {
                    "location": ["db_object_id"],
                    "message": "A supplied domain validation message",
                    "type": "missing",
                }
            ],
        }
    }


def test_malformed_query_api_error_keeps_its_stable_code() -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/resource")
    def route() -> None:
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="unknown_query_parameter",
            message="Unknown query parameter: unexpected",
        )

    response = TestClient(app).get("/resource")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {
        "error": {
            "code": "unknown_query_parameter",
            "message": "Unknown query parameter: unexpected",
        }
    }


def test_unexpected_request_validation_error_uses_the_stable_envelope() -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/resource")
    def route(limit: Annotated[int, Query(ge=1)]) -> None:
        del limit

    response = TestClient(app).get("/resource", params={"limit": "zero"})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    error = response.json()["error"]
    assert error["code"] == "request_validation_error"
    assert error["message"] == "Request validation failed"
    assert len(error["details"]) == 1
    issue = error["details"][0]
    assert issue["location"] == ["query", "limit"]
    assert issue["type"] == "int_parsing"
    assert isinstance(issue["message"], str) and issue["message"]
