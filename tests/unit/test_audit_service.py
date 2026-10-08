"""Verify the audit events recorded by `AuditService` without database access."""

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

    service.record_change_set_transition(
        action=AuditAction.CHANGE_SET_ACCEPTED,
        context=_context(),
        change_set_id=change_set_id,
        operation="update",
        annotation_id=annotation_id,
        annotation_version=3,
    )

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


@pytest.mark.parametrize(
    ("method", "action"),
    [
        ("record_ontology_refreshed", AuditAction.ONTOLOGY_REFRESHED),
        ("record_entity_refreshed", AuditAction.ENTITY_REFRESHED),
        ("record_entity_retired", AuditAction.ENTITY_RETIRED),
        ("record_authorization_refreshed", AuditAction.AUTHORIZATION_REFRESHED),
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
