"""Test durable audit events and their transaction boundaries."""

from uuid import UUID

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.persistence.models import (
    AnnotationDuplicateReferenceRecord,
    AnnotationMultivaluedFieldValueRecord,
    AnnotationRecord,
    AnnotationVersionRecord,
    AuditEventRecord,
)

pytestmark = pytest.mark.usefixtures("active_annotation_subjects")


VALID_ANNOTATION = {
    "db_object_id": "UniProtKB:P12345",
    "relation": "RO:0002331",
    "ontology_class_id": "GO:0008150",
    "references": ["PMID:1"],
    "evidence_type": "ECO:0000314",
    "annotation_date": "2026-09-15",
    "assigned_by": "GO_Central",
}


def _stored_audit_events(
    session_factory: sessionmaker[Session],
) -> tuple[AuditEventRecord, ...]:
    with session_factory() as session:
        return tuple(
            session.scalars(
                select(AuditEventRecord).order_by(AuditEventRecord.created_at)
            )
        )


def test_direct_annotation_mutations_record_successful_audit_events(
    integration_api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Create, update, and delete each record the actor and resulting version."""
    created = integration_api_client.post(
        "/annotations",
        json={"owning_group_id": "group-1", "annotation": VALID_ANNOTATION},
    )
    assert created.status_code == status.HTTP_201_CREATED
    annotation_id = UUID(created.json()["annotation_id"])

    updated = integration_api_client.patch(
        f"/annotations/{annotation_id}",
        headers={"If-Match": '"1"'},
        json={"assigned_by": "Updated_Source"},
    )
    assert updated.status_code == status.HTTP_200_OK

    deleted = integration_api_client.delete(
        f"/annotations/{annotation_id}",
        headers={"If-Match": '"2"'},
    )
    assert deleted.status_code == status.HTTP_204_NO_CONTENT

    events = _stored_audit_events(session_factory)
    assert [audit_event.action for audit_event in events] == [
        "annotation.created",
        "annotation.updated",
        "annotation.deleted",
    ]
    assert [audit_event.annotation_version for audit_event in events] == [1, 2, 3]
    assert all(audit_event.actor_id == "api-test-user" for audit_event in events)
    assert all(audit_event.result == "success" for audit_event in events)
    assert all(audit_event.annotation_id == annotation_id for audit_event in events)
    assert all(
        audit_event.token_id == "00000000-0000-0000-0000-000000000501"
        for audit_event in events
    )
    assert all(audit_event.token_name == "Integration tests" for audit_event in events)
    assert all(audit_event.selected_role == "admin" for audit_event in events)
    assert all(audit_event.selected_scope == "global" for audit_event in events)
    assert all(audit_event.selected_group_id is None for audit_event in events)
    assert all(
        audit_event.details == {"change_source": "api"} for audit_event in events
    )


def test_rejected_direct_mutations_do_not_record_successful_audit_events(
    integration_api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Validation, duplicate, and stale-write rejection add no success events."""
    created = integration_api_client.post(
        "/annotations",
        json={"owning_group_id": "group-1", "annotation": VALID_ANNOTATION},
    )
    assert created.status_code == status.HTTP_201_CREATED
    annotation_id = UUID(created.json()["annotation_id"])

    invalid = integration_api_client.post(
        "/annotations",
        json={
            "owning_group_id": "group-1",
            "annotation": {**VALID_ANNOTATION, "db_object_id": None},
        },
    )
    duplicate = integration_api_client.post(
        "/annotations",
        json={"owning_group_id": "group-1", "annotation": VALID_ANNOTATION},
    )
    stale = integration_api_client.patch(
        f"/annotations/{annotation_id}",
        headers={"If-Match": '"9"'},
        json={"assigned_by": "Stale_Source"},
    )

    assert invalid.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert duplicate.status_code == status.HTTP_409_CONFLICT
    assert stale.status_code == status.HTTP_412_PRECONDITION_FAILED
    events = _stored_audit_events(session_factory)
    assert [
        (audit_event.action, audit_event.annotation_version) for audit_event in events
    ] == [("annotation.created", 1)]


def test_audit_write_failure_rolls_back_the_complete_annotation_create(
    integration_api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """An audit insertion failure returns `internal_error` and stores nothing."""

    def reject_audit_insert(*_: object) -> None:
        raise RuntimeError("audit storage unavailable")

    event.listen(AuditEventRecord, "before_insert", reject_audit_insert)
    try:
        response = integration_api_client.post(
            "/annotations",
            json={
                "owning_group_id": "group-1",
                "annotation": VALID_ANNOTATION,
            },
        )
    finally:
        event.remove(AuditEventRecord, "before_insert", reject_audit_insert)

    with session_factory() as session:
        counts = tuple(
            session.scalar(select(func.count()).select_from(record_type))
            for record_type in (
                AnnotationRecord,
                AnnotationVersionRecord,
                AnnotationMultivaluedFieldValueRecord,
                AnnotationDuplicateReferenceRecord,
                AuditEventRecord,
            )
        )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json()["error"]["code"] == "internal_error"
    assert counts == (0, 0, 0, 0, 0)
