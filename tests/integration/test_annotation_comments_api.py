"""Test annotation comment workflows through the public API."""

from uuid import UUID, uuid4

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.api.dependencies import get_authenticated_context
from standard_annotation_backend.domain.auth import (
    AuthorizationRole,
    AuthorizationScope,
    RequestContext,
)
from standard_annotation_backend.main import app
from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AuditEventRecord,
)

pytestmark = pytest.mark.usefixtures("active_annotation_subjects")

VALID_ANNOTATION = {
    "db_object_id": "UniProtKB:P12345",
    "relation": "RO:0002331",
    "ontology_class_id": "GO:0008150",
    "references": ["PMID:1"],
    "evidence_type": "ECO:0000314",
    "annotation_date": "2026-09-21",
    "assigned_by": "GO_Central",
}


def _create_annotation(
    client: TestClient,
    *,
    db_object_id: str = "UniProtKB:P12345",
) -> dict[str, object]:
    response = client.post(
        "/annotations",
        json={
            "owning_group_id": "group-1",
            "annotation": {**VALID_ANNOTATION, "db_object_id": db_object_id},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()


def _use_context(
    *,
    actor_id: str,
    role: AuthorizationRole,
    scope: AuthorizationScope = AuthorizationScope.GLOBAL,
    group_id: str | None = None,
) -> RequestContext:
    context = RequestContext(
        actor_id=actor_id,
        token_id=uuid4(),
        token_name=f"{actor_id} token",
        role=role,
        scope=scope,
        group_id=group_id,
    )
    app.dependency_overrides[get_authenticated_context] = lambda: context
    return context


def _create_comment(
    client: TestClient,
    annotation_id: object,
    *,
    body: str = "Initial comment",
) -> dict[str, object]:
    response = client.post(
        f"/annotations/{annotation_id}/comments",
        json={"body": body},
    )
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()


def test_create_and_list_comments_pin_the_current_annotation_version(
    integration_api_client: TestClient,
) -> None:
    """Create derives the current version and list returns the stored comment."""
    annotation = _create_annotation(integration_api_client)
    annotation_id = annotation["annotation_id"]

    created = integration_api_client.post(
        f"/annotations/{annotation_id}/comments",
        json={"body": "Check the supporting reference."},
    )

    assert created.status_code == status.HTTP_201_CREATED
    body = created.json()
    comment_id = UUID(body["comment_id"])
    assert body == {
        "comment_id": str(comment_id),
        "annotation_id": annotation_id,
        "annotation_version": 1,
        "body": "Check the supporting reference.",
        "created_by": "api-test-user",
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    assert body["created_at"] == body["updated_at"]
    assert created.headers["location"] == (
        f"/annotations/{annotation_id}/comments/{comment_id}"
    )

    listed = integration_api_client.get(
        f"/annotations/{annotation_id}/comments?limit=20&offset=0"
    )

    assert listed.status_code == status.HTTP_200_OK
    assert listed.json() == {
        "items": [body],
        "total": 1,
        "limit": 20,
        "offset": 0,
    }


def test_create_pins_the_actual_current_annotation_version(
    integration_api_client: TestClient,
) -> None:
    """A comment created after an annotation update records that new version."""
    annotation = _create_annotation(integration_api_client)
    annotation_id = annotation["annotation_id"]
    updated = integration_api_client.patch(
        f"/annotations/{annotation_id}",
        headers={"If-Match": '"1"'},
        json={"assigned_by": "MGI"},
    )

    assert updated.status_code == status.HTTP_200_OK
    assert updated.json()["version"] == 2

    comment = _create_comment(integration_api_client, annotation_id)

    assert comment["annotation_version"] == 2


def test_comment_listing_paginates_visible_comments(
    integration_api_client: TestClient,
) -> None:
    """List returns the requested stable slice and the full visible count."""
    annotation = _create_annotation(integration_api_client)
    annotation_id = annotation["annotation_id"]
    comments = [
        _create_comment(integration_api_client, annotation_id, body=f"Comment {index}")
        for index in range(3)
    ]

    response = integration_api_client.get(
        f"/annotations/{annotation_id}/comments?limit=1&offset=1"
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "items": [comments[1]],
        "total": 3,
        "limit": 1,
        "offset": 1,
    }


def test_comment_body_cannot_supply_an_annotation_version(
    integration_api_client: TestClient,
) -> None:
    """Clients cannot choose the annotation version attached to a comment."""
    annotation = _create_annotation(integration_api_client)

    response = integration_api_client.post(
        f"/annotations/{annotation['annotation_id']}/comments",
        json={"body": "Comment", "annotation_version": 99},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"
    assert response.json()["error"]["details"][0]["location"] == [
        "body",
        "annotation_version",
    ]


def test_comment_body_accepts_nonblank_multiline_text(
    integration_api_client: TestClient,
) -> None:
    """A newline does not make otherwise nonblank comment text invalid."""
    annotation = _create_annotation(integration_api_client)

    response = integration_api_client.post(
        f"/annotations/{annotation['annotation_id']}/comments",
        json={"body": "First observation\nSecond observation"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["body"] == "First observation\nSecond observation"


def test_parent_annotation_scope_controls_comment_access(
    integration_api_client: TestClient,
) -> None:
    """Read access to the parent gates comment creation and listing."""
    annotation = _create_annotation(integration_api_client)
    annotation_id = annotation["annotation_id"]
    _use_context(
        actor_id="outside-reader",
        role=AuthorizationRole.READ,
        scope=AuthorizationScope.GROUP,
        group_id="group-2",
    )

    denied_list = integration_api_client.get(f"/annotations/{annotation_id}/comments")
    denied_create = integration_api_client.post(
        f"/annotations/{annotation_id}/comments",
        json={"body": "Outside scope"},
    )

    assert denied_list.status_code == status.HTTP_404_NOT_FOUND
    assert denied_create.status_code == status.HTTP_404_NOT_FOUND

    _use_context(
        actor_id="group-reader",
        role=AuthorizationRole.READ,
        scope=AuthorizationScope.GROUP,
        group_id="group-1",
    )
    created = _create_comment(integration_api_client, annotation_id, body="In scope")
    edited = integration_api_client.patch(
        f"/annotations/{annotation_id}/comments/{created['comment_id']}",
        json={"body": "Author edit with read role"},
    )

    assert edited.status_code == status.HTTP_200_OK
    assert edited.json()["body"] == "Author edit with read role"


def test_author_can_edit_and_soft_delete_without_changing_annotation_history(
    integration_api_client: TestClient,
) -> None:
    """Author mutations update the comment while preserving annotation history."""
    annotation = _create_annotation(integration_api_client)
    annotation_id = annotation["annotation_id"]
    comment = _create_comment(integration_api_client, annotation_id)
    path = f"/annotations/{annotation_id}/comments/{comment['comment_id']}"

    edited = integration_api_client.patch(path, json={"body": "Corrected comment"})

    assert edited.status_code == status.HTTP_200_OK
    assert edited.json()["body"] == "Corrected comment"
    assert edited.json()["created_by"] == "api-test-user"
    assert edited.json()["annotation_version"] == 1
    assert edited.json()["updated_at"] > comment["updated_at"]

    deleted = integration_api_client.delete(path)

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert deleted.content == b""
    listed = integration_api_client.get(f"/annotations/{annotation_id}/comments")
    assert listed.json()["items"] == []
    assert listed.json()["total"] == 0
    history = integration_api_client.get(f"/annotations/{annotation_id}/versions")
    assert history.json()["total"] == 1

    repeated = integration_api_client.delete(path)
    assert repeated.status_code == status.HTTP_404_NOT_FOUND
    assert repeated.json()["error"]["code"] == "comment_not_found"


def test_non_author_cannot_edit_or_delete_a_comment(
    integration_api_client: TestClient,
) -> None:
    """A non-author without the admin role cannot mutate another user's comment."""
    annotation = _create_annotation(integration_api_client)
    comment = _create_comment(integration_api_client, annotation["annotation_id"])
    path = (
        f"/annotations/{annotation['annotation_id']}/comments/{comment['comment_id']}"
    )
    _use_context(actor_id="other-user", role=AuthorizationRole.EDIT)

    edited = integration_api_client.patch(path, json={"body": "Not allowed"})
    deleted = integration_api_client.delete(path)

    assert edited.status_code == status.HTTP_403_FORBIDDEN
    assert edited.json()["error"]["code"] == "permission_denied"
    assert deleted.status_code == status.HTTP_403_FORBIDDEN
    assert deleted.json()["error"]["code"] == "permission_denied"


def test_inaccessible_parent_does_not_disclose_comment_existence(
    integration_api_client: TestClient,
) -> None:
    """Existing and unknown comments look identical below an inaccessible parent."""
    annotation = _create_annotation(integration_api_client)
    comment = _create_comment(integration_api_client, annotation["annotation_id"])
    _use_context(
        actor_id="outside-admin",
        role=AuthorizationRole.ADMIN,
        scope=AuthorizationScope.GROUP,
        group_id="group-2",
    )
    base_path = f"/annotations/{annotation['annotation_id']}/comments"

    existing = integration_api_client.patch(
        f"{base_path}/{comment['comment_id']}",
        json={"body": "Outside scope"},
    )
    unknown = integration_api_client.patch(
        f"{base_path}/{uuid4()}",
        json={"body": "Outside scope"},
    )

    assert existing.status_code == status.HTTP_404_NOT_FOUND
    assert unknown.status_code == status.HTTP_404_NOT_FOUND
    assert (
        existing.json()
        == unknown.json()
        == {
            "error": {
                "code": "annotation_not_found",
                "message": "Annotation was not found",
            }
        }
    )


def test_annotation_admin_can_edit_and_delete_another_users_comment(
    integration_api_client: TestClient,
) -> None:
    """An administrator within annotation scope can moderate another author."""
    annotation = _create_annotation(integration_api_client)
    comment = _create_comment(integration_api_client, annotation["annotation_id"])
    path = (
        f"/annotations/{annotation['annotation_id']}/comments/{comment['comment_id']}"
    )
    _use_context(actor_id="administrator", role=AuthorizationRole.ADMIN)

    edited = integration_api_client.patch(path, json={"body": "Admin correction"})
    deleted = integration_api_client.delete(path)

    assert edited.status_code == status.HTTP_200_OK
    assert edited.json()["body"] == "Admin correction"
    assert deleted.status_code == status.HTTP_204_NO_CONTENT


def test_comment_mutation_rejects_a_different_parent_annotation(
    integration_api_client: TestClient,
) -> None:
    """A comment ID cannot be used below a different annotation resource."""
    first = _create_annotation(integration_api_client)
    comment = _create_comment(integration_api_client, first["annotation_id"])
    second = _create_annotation(
        integration_api_client,
        db_object_id="UniProtKB:Q99999",
    )

    response = integration_api_client.patch(
        f"/annotations/{second['annotation_id']}/comments/{comment['comment_id']}",
        json={"body": "Wrong parent"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["error"]["code"] == "comment_not_found"


def test_comment_mutations_record_authenticated_audit_context(
    integration_api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Create, edit, and delete persist actor, token, and target attribution."""
    annotation = _create_annotation(integration_api_client)
    context = _use_context(actor_id="commenter", role=AuthorizationRole.EDIT)
    comment = _create_comment(integration_api_client, annotation["annotation_id"])
    path = (
        f"/annotations/{annotation['annotation_id']}/comments/{comment['comment_id']}"
    )
    edited = integration_api_client.patch(path, json={"body": "Edited"})
    deleted = integration_api_client.delete(path)

    assert edited.status_code == status.HTTP_200_OK
    assert deleted.status_code == status.HTTP_204_NO_CONTENT

    with session_factory() as session:
        events = tuple(
            session.scalars(
                select(AuditEventRecord)
                .where(AuditEventRecord.comment_id == UUID(str(comment["comment_id"])))
                .order_by(AuditEventRecord.created_at, AuditEventRecord.audit_event_id)
            )
        )

    assert [event.action for event in events] == [
        "annotation_comment.created",
        "annotation_comment.updated",
        "annotation_comment.deleted",
    ]
    assert all(event.actor_id == "commenter" for event in events)
    assert all(event.token_id == str(context.token_id) for event in events)
    assert all(event.token_name == "commenter token" for event in events)
    assert all(event.selected_role == "edit" for event in events)
    assert all(event.selected_scope == "global" for event in events)
    assert all(
        event.annotation_id == UUID(str(annotation["annotation_id"]))
        for event in events
    )
    assert all(event.annotation_version == 1 for event in events)


@pytest.mark.parametrize("operation", ["create", "edit", "delete"])
def test_audit_failure_rolls_back_the_complete_comment_mutation(
    integration_api_client: TestClient,
    session_factory: sessionmaker[Session],
    operation: str,
) -> None:
    """A failed audit insert rolls back its comment mutation atomically."""
    annotation = _create_annotation(integration_api_client)
    annotation_id = annotation["annotation_id"]
    comment: dict[str, object] | None = None
    if operation != "create":
        comment = _create_comment(
            integration_api_client,
            annotation_id,
            body="Original body",
        )

    with session_factory() as session:
        audit_count_before = session.scalar(
            select(func.count()).select_from(AuditEventRecord)
        )

    def reject_audit_insert(*_: object) -> None:
        raise RuntimeError("audit storage unavailable")

    event.listen(AuditEventRecord, "before_insert", reject_audit_insert)
    try:
        with pytest.raises(RuntimeError, match="audit storage unavailable"):
            if operation == "create":
                integration_api_client.post(
                    f"/annotations/{annotation_id}/comments",
                    json={"body": "Uncommitted comment"},
                )
            elif operation == "edit":
                assert comment is not None
                integration_api_client.patch(
                    f"/annotations/{annotation_id}/comments/{comment['comment_id']}",
                    json={"body": "Uncommitted edit"},
                )
            else:
                assert comment is not None
                integration_api_client.delete(
                    f"/annotations/{annotation_id}/comments/{comment['comment_id']}"
                )
    finally:
        event.remove(AuditEventRecord, "before_insert", reject_audit_insert)

    with session_factory() as session:
        stored_comments = tuple(
            session.scalars(
                select(AnnotationCommentRecord).where(
                    AnnotationCommentRecord.annotation_id == UUID(str(annotation_id))
                )
            )
        )
        audit_count_after = session.scalar(
            select(func.count()).select_from(AuditEventRecord)
        )

    assert audit_count_after == audit_count_before
    if operation == "create":
        assert stored_comments == ()
    else:
        assert len(stored_comments) == 1
        assert stored_comments[0].body == "Original body"
        assert stored_comments[0].deleted_at is None
