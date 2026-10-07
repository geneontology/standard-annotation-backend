"""Verify the audit events recorded by `AuditService` without database access."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from standard_annotation_backend.domain.annotations import ChangeSource
from standard_annotation_backend.domain.audit import AuditAction, AuditResult
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
    RequestContext,
)
from standard_annotation_backend.persistence.models import AuditEventRecord
from standard_annotation_backend.persistence.repositories import AuditRepository
from standard_annotation_backend.services.audit_service import AuditService

_RECORD = cast(AuditEventRecord, object())


class _RecordingRepository:
    """Capture the values passed to `AuditRepository.record`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def record(self, **values: object) -> AuditEventRecord:
        self.calls.append(values)
        return _RECORD


def _service() -> tuple[AuditService, _RecordingRepository]:
    repository = _RecordingRepository()
    return AuditService(cast(AuditRepository, repository)), repository


def _context() -> RequestContext:
    return RequestContext(
        actor_id="actor",
        role=AuthorizationRole.EDIT,
        scope=AuthorizationScope.GROUP,
        group_id="group",
        token_id=UUID("00000000-0000-0000-0000-000000000001"),
        token_name="Curation token",
    )


def _request_values() -> dict[str, object]:
    return {
        "actor_id": "actor",
        "token_id": "00000000-0000-0000-0000-000000000001",
        "token_name": "Curation token",
        "selected_role": "edit",
        "selected_scope": "group",
        "selected_group_id": "group",
        "result": AuditResult.SUCCESS,
    }


def test_change_set_transition_records_request_identity_and_operation() -> None:
    """A change-set event records the request identity and the proposed operation."""
    service, repository = _service()
    change_set_id, annotation_id = uuid4(), uuid4()

    result = service.record_change_set_transition(
        action=AuditAction.CHANGE_SET_ACCEPTED,
        context=_context(),
        change_set_id=change_set_id,
        operation="update",
        annotation_id=annotation_id,
        annotation_version=3,
    )

    assert result is _RECORD
    assert repository.calls == [
        {
            "action": AuditAction.CHANGE_SET_ACCEPTED,
            **_request_values(),
            "change_set_id": change_set_id,
            "annotation_id": annotation_id,
            "annotation_version": 3,
            "details": {
                "change_source": ChangeSource.CHANGE_SET,
                "operation": "update",
            },
        }
    ]


def test_annotation_mutation_records_request_identity_and_version() -> None:
    """A direct annotation change records the request identity and new version."""
    service, repository = _service()
    annotation_id = uuid4()

    service.record_annotation_mutation(
        action=AuditAction.ANNOTATION_UPDATED,
        context=_context(),
        annotation_id=annotation_id,
        annotation_version=2,
    )

    assert repository.calls == [
        {
            "action": AuditAction.ANNOTATION_UPDATED,
            **_request_values(),
            "annotation_id": annotation_id,
            "annotation_version": 2,
            "details": {"change_source": ChangeSource.API},
        }
    ]


def test_comment_mutation_records_request_identity_and_comment() -> None:
    """A comment change records the request identity and the affected comment."""
    service, repository = _service()
    annotation_id, comment_id = uuid4(), uuid4()

    service.record_annotation_comment_mutation(
        action=AuditAction.ANNOTATION_COMMENT_CREATED,
        context=_context(),
        annotation_id=annotation_id,
        annotation_version=1,
        comment_id=comment_id,
    )

    assert repository.calls == [
        {
            "action": AuditAction.ANNOTATION_COMMENT_CREATED,
            **_request_values(),
            "annotation_id": annotation_id,
            "annotation_version": 1,
            "comment_id": comment_id,
            "details": {"change_source": ChangeSource.API},
        }
    ]


@pytest.mark.parametrize(
    ("method", "action"),
    [
        ("record_ontology_refreshed", AuditAction.ONTOLOGY_REFRESHED),
        ("record_entity_refreshed", AuditAction.ENTITY_REFRESHED),
        ("record_entity_retired", AuditAction.ENTITY_RETIRED),
    ],
)
def test_refresh_summaries_record_their_action_job_and_details(
    method: str, action: AuditAction
) -> None:
    """Each refresh summary records its own action with the job and details."""
    service, repository = _service()
    job_id = uuid4()

    getattr(service, method)(actor_id="refresher", job_id=job_id, details={"n": 1})

    assert repository.calls == [
        {
            "action": action,
            "actor_id": "refresher",
            "result": AuditResult.SUCCESS,
            "job_id": job_id,
            "details": {"n": 1},
        }
    ]


def test_token_created_records_owner_token_assignment_and_expiration() -> None:
    """Token creation is attributed to its owner and names the new token."""
    service, repository = _service()
    user_id, token_id, assignment_id = uuid4(), uuid4(), uuid4()
    expires_at = datetime(2026, 12, 31, 12, 0, tzinfo=UTC)

    service.record_token_created(
        user_id=user_id,
        token_id=token_id,
        assignment_id=assignment_id,
        expires_at=expires_at,
    )

    assert repository.calls == [
        {
            "action": AuditAction.TOKEN_CREATED,
            "actor_id": str(user_id),
            "result": AuditResult.SUCCESS,
            "details": {
                "token_id": str(token_id),
                "assignment_id": str(assignment_id),
                "expires_at": "2026-12-31T12:00:00+00:00",
            },
        }
    ]


def test_token_revoked_records_owner_and_token() -> None:
    """Token revocation is attributed to its owner and names the revoked token."""
    service, repository = _service()
    user_id, token_id = uuid4(), uuid4()

    service.record_token_revoked(user_id=user_id, token_id=token_id)

    assert repository.calls == [
        {
            "action": AuditAction.TOKEN_REVOKED,
            "actor_id": str(user_id),
            "result": AuditResult.SUCCESS,
            "details": {"token_id": str(token_id)},
        }
    ]


def test_authorization_refreshed_records_actor_job_and_details() -> None:
    """An applied authorization refresh records its actor, job, and summary."""
    service, repository = _service()
    job_id = uuid4()

    service.record_authorization_refreshed(
        actor_id="refresher", job_id=job_id, details={"users": 2}
    )

    assert repository.calls == [
        {
            "action": AuditAction.AUTHORIZATION_REFRESHED,
            "actor_id": "refresher",
            "result": AuditResult.SUCCESS,
            "job_id": job_id,
            "details": {"users": 2},
        }
    ]
