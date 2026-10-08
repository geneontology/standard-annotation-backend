"""Verify API resources convert service results into the documented responses."""

from datetime import UTC, datetime
from uuid import UUID

from standard_annotation_backend.api.models import (
    AnnotationCommentResource,
    AnnotationResource,
    AnnotationVersionResource,
    JobResource,
    TokenContextResource,
    TokenCreatedResponse,
    TokenResource,
)
from standard_annotation_backend.domain.annotations import Annotation, ChangeSource
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
)
from standard_annotation_backend.domain.jobs import JobStatus, JobType
from standard_annotation_backend.domain.tokens import TokenMetadata
from standard_annotation_backend.services.annotation_service import (
    AnnotationVersion,
    CurrentAnnotation,
)
from standard_annotation_backend.services.comment_service import AnnotationComment
from standard_annotation_backend.services.job_service import Job
from standard_annotation_backend.services.token_service import (
    CreatedToken,
    TokenContext,
)

_ID_1 = UUID("00000000-0000-0000-0000-000000000001")
_ID_2 = UUID("00000000-0000-0000-0000-000000000002")
_ID_3 = UUID("00000000-0000-0000-0000-000000000003")
_CREATED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_UPDATED = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_annotation_resource_carries_current_annotation_fields(
    validated_annotation: Annotation,
) -> None:
    """An annotation response contains the current annotation and its metadata."""
    result = CurrentAnnotation(
        annotation_id=_ID_1,
        version=3,
        owning_group_id="MGI",
        record_origin="direct",
        source_import_job_id=_ID_2,
        created_at=_CREATED,
        updated_at=_UPDATED,
        annotation=validated_annotation,
    )

    assert AnnotationResource.from_service(result).model_dump(mode="json") == {
        "annotation_id": str(_ID_1),
        "version": 3,
        "owning_group_id": "MGI",
        "record_origin": "direct",
        "source_import_job_id": str(_ID_2),
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-02T12:00:00Z",
        "annotation": validated_annotation.model_dump(mode="json"),
    }


def test_job_resource_omits_worker_parameters() -> None:
    """A job response reports status and results but not worker parameters."""
    job = Job(
        job_id=_ID_1,
        job_type=JobType.ONTOLOGY_REFRESH,
        status=JobStatus.SUCCEEDED,
        requested_by="curator",
        parameters={"secret": "value"},
        progress={"phase": "done"},
        warnings=("careful",),
        result={"unchanged": False},
        error=None,
        created_at=_CREATED,
        updated_at=_UPDATED,
        started_at=_CREATED,
        completed_at=_UPDATED,
    )

    assert JobResource.from_service(job).model_dump(mode="json") == {
        "job_id": str(_ID_1),
        "job_type": "ontology_refresh",
        "status": "succeeded",
        "requested_by": "curator",
        "progress": {"phase": "done"},
        "warnings": ["careful"],
        "result": {"unchanged": False},
        "error": None,
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-02T12:00:00Z",
        "started_at": "2026-09-01T12:00:00Z",
        "completed_at": "2026-09-02T12:00:00Z",
    }


def test_comment_resource_carries_comment_fields() -> None:
    """A comment response contains every field of the comment."""
    comment = AnnotationComment(
        comment_id=_ID_1,
        annotation_id=_ID_2,
        annotation_version=4,
        body="Looks right",
        created_by="curator",
        created_at=_CREATED,
        updated_at=_UPDATED,
    )

    assert AnnotationCommentResource.from_service(comment).model_dump(mode="json") == {
        "comment_id": str(_ID_1),
        "annotation_id": str(_ID_2),
        "annotation_version": 4,
        "body": "Looks right",
        "created_by": "curator",
        "created_at": "2026-09-01T12:00:00Z",
        "updated_at": "2026-09-02T12:00:00Z",
    }


def test_version_resource_carries_saved_version_fields(
    validated_annotation: Annotation,
) -> None:
    """A version response contains the saved annotation and change details."""
    version = AnnotationVersion(
        annotation_id=_ID_1,
        version=2,
        is_deleted=False,
        actor_id="curator",
        change_source=ChangeSource.API,
        created_at=_CREATED,
        annotation=validated_annotation,
    )

    assert AnnotationVersionResource.from_service(version).model_dump(mode="json") == {
        "annotation_id": str(_ID_1),
        "version": 2,
        "is_deleted": False,
        "actor_id": "curator",
        "change_source": "api",
        "created_at": "2026-09-01T12:00:00Z",
        "annotation": validated_annotation.model_dump(mode="json"),
    }


def test_token_context_resource_carries_context_fields() -> None:
    """A token context response names the assignment, role, scope, and group."""
    context = TokenContext(
        assignment_id=_ID_1,
        role=AuthorizationRole.EDIT,
        scope=AuthorizationScope.GROUP,
        group_id="MGI",
    )

    assert TokenContextResource.from_service(context).model_dump(mode="json") == {
        "assignment_id": str(_ID_1),
        "role": "edit",
        "scope": "group",
        "group_id": "MGI",
    }


def _metadata() -> TokenMetadata:
    return TokenMetadata(
        token_id=_ID_1,
        user_id=_ID_2,
        assignment_id=_ID_3,
        name="Notebook",
        created_at=_CREATED,
        expires_at=_UPDATED,
        last_used_at=None,
        revoked_at=None,
        role=AuthorizationRole.EDIT,
        scope=AuthorizationScope.GROUP,
        group_id="MGI",
        assignment_is_active=True,
    )


_TOKEN_FIELDS = {
    "token_id": str(_ID_1),
    "user_id": str(_ID_2),
    "assignment_id": str(_ID_3),
    "name": "Notebook",
    "created_at": "2026-09-01T12:00:00Z",
    "expires_at": "2026-09-02T12:00:00Z",
    "last_used_at": None,
    "revoked_at": None,
    "role": "edit",
    "scope": "group",
    "group_id": "MGI",
    "assignment_is_active": True,
}


def test_token_resource_carries_token_metadata() -> None:
    """A token response contains the metadata and no secret."""
    assert (
        TokenResource.from_service(_metadata()).model_dump(mode="json") == _TOKEN_FIELDS
    )


def test_token_created_response_adds_secret_to_token_metadata() -> None:
    """The creation response contains the raw token and every metadata field."""
    created = CreatedToken("sab_secret", _metadata())

    assert TokenCreatedResponse.from_service(created).model_dump(mode="json") == {
        "token": "sab_secret",
        **_TOKEN_FIELDS,
    }
