"""Test proposal and review HTTP contracts against PostgreSQL."""

from uuid import UUID

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.persistence.models import (
    AuditEventRecord,
    ChangeSetRecord,
)

ANNOTATION = {
    "db_object_id": "UniProtKB:P12345",
    "relation": "RO:0002331",
    "ontology_class_id": "GO:0008150",
    "references": ["PMID:1"],
    "evidence_type": "ECO:0000314",
    "annotation_date": "2026-09-15",
    "assigned_by": "GO_Central",
}
UNKNOWN_ID = "00000000-0000-0000-0000-000000000501"


def _create_proposal(client: TestClient, **overrides: object) -> dict:
    response = client.post(
        "/change-sets",
        json={
            "operation": "create",
            "owning_group_id": "group-1",
            "annotation": ANNOTATION,
            "reason": "Add evidence",
            **overrides,
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()


def _create_annotation(client: TestClient) -> dict:
    response = client.post(
        "/annotations",
        json={"owning_group_id": "group-1", "annotation": ANNOTATION},
    )
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()


def test_create_read_preview_and_accept(integration_api_client: TestClient) -> None:
    """A proposal remains separate from annotations until accepted with identity."""
    client = integration_api_client
    response = client.post(
        "/change-sets",
        json={
            "operation": "create",
            "owning_group_id": "group-1",
            "annotation": ANNOTATION,
            "reason": "Add evidence",
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    proposal = response.json()
    UUID(proposal["change_set_id"])
    path = f"/change-sets/{proposal['change_set_id']}"
    assert response.headers["location"] == path
    assert proposal["state"] == "proposed"
    assert proposal["proposed_by"] == "api-test-user"
    assert proposal["owning_group_id"] == "group-1"
    assert proposal["annotation_payload"] == ANNOTATION
    assert proposal["preview"] is None
    assert client.get(path).json() == proposal
    assert client.get("/annotations").json()["total"] == 0

    preview = client.post(f"{path}/preview")
    assert preview.status_code == status.HTTP_200_OK
    assert preview.json()["can_accept"] is True
    assert preview.json()["validation_errors"] == []
    assert preview.json()["duplicate_peer_ids"] == []
    assert client.get(path).json()["preview"] == preview.json()
    assert client.get("/annotations").json()["total"] == 0

    accepted = client.post(f"{path}/accept", json={"review_reason": "Verified"})
    assert accepted.status_code == status.HTTP_200_OK
    result = accepted.json()
    assert result["version"] == 1
    assert result["is_deleted"] is False
    assert result["annotation"]["references"] == ["PMID:1"]
    assert result["change_set"]["state"] == "accepted"
    assert result["change_set"]["reviewed_by"] == "api-test-user"
    assert result["change_set"]["review_reason"] == "Verified"
    assert result["change_set"]["result_annotation_version"] == 1
    assert result["change_set"]["annotation_id"] == result["annotation_id"]
    assert client.get(path).json() == result["change_set"]
    assert (
        client.get(f"/annotations/{result['annotation_id']}").json()["annotation"]
        == result["annotation"]
    )


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_accept_target_proposal(
    integration_api_client: TestClient, operation: str
) -> None:
    """Update and delete acceptance return the resulting version and deletion flag."""
    client = integration_api_client
    target = _create_annotation(client)
    body = {
        "operation": operation,
        "annotation_id": target["annotation_id"],
        "base_version": 1,
        "reason": "Curator correction",
    }
    if operation == "update":
        body["patch"] = [{"op": "replace", "path": "/references", "value": ["PMID:2"]}]
    proposal = client.post("/change-sets", json=body)
    assert proposal.status_code == status.HTTP_201_CREATED
    path = proposal.headers["location"]
    assert proposal.json()["owning_group_id"] == "group-1"
    preview = client.post(f"{path}/preview")
    assert preview.status_code == status.HTTP_200_OK
    assert preview.json()["before_annotation"]["references"] == ["PMID:1"]
    assert preview.json()["current_version"] == 1
    assert preview.json()["can_accept"] is True
    accepted = client.post(f"{path}/accept")
    assert accepted.status_code == status.HTTP_200_OK
    assert accepted.json()["annotation_id"] == target["annotation_id"]
    assert accepted.json()["version"] == 2
    assert accepted.json()["is_deleted"] is (operation == "delete")
    assert accepted.json()["change_set"]["state"] == "accepted"
    assert accepted.json()["change_set"]["review_reason"] is None
    assert accepted.json()["annotation"]["references"] == (
        ["PMID:2"] if operation == "update" else ["PMID:1"]
    )


@pytest.mark.parametrize("action", ["preview", "accept", "reject"])
def test_terminal_proposal_rejects_review(
    integration_api_client: TestClient, action: str
) -> None:
    """Rejected proposals remain readable and reject further review operations."""
    client = integration_api_client
    proposal = _create_proposal(client)
    path = f"/change-sets/{proposal['change_set_id']}"
    rejected = client.post(f"{path}/reject", json={"review_reason": "No support"})
    assert rejected.status_code == status.HTTP_200_OK
    assert rejected.json()["state"] == "rejected"
    assert rejected.json()["review_reason"] == "No support"
    assert rejected.json()["reviewed_by"] == "api-test-user"
    assert client.get(path).json() == rejected.json()
    response = client.post(f"{path}/{action}", json={"review_reason": "Again"})
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["error"]["code"] == "change_set_not_proposed"
    assert client.get("/annotations").json()["total"] == 0


@pytest.mark.parametrize("action", [None, "preview", "accept", "reject"])
def test_missing_change_set(
    integration_api_client: TestClient, action: str | None
) -> None:
    """Every resource operation returns a stable error for an unknown proposal."""
    path = f"/change-sets/{UNKNOWN_ID}"
    response = (
        integration_api_client.get(path)
        if action is None
        else integration_api_client.post(
            f"{path}/{action}", json={"review_reason": "No"}
        )
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["error"]["code"] == "change_set_not_found"


@pytest.mark.parametrize(
    "overrides",
    [
        {"operation": "unknown"},
        {"reason": " \n "},
        {"owning_group_id": " \t "},
        {"annotation": []},
        {"annotation": None},
        {"base_version": 1},
        {"extra": True},
    ],
)
def test_create_request_boundary(
    integration_api_client: TestClient, overrides: dict[str, object]
) -> None:
    """Invalid proposal envelopes use the standard typed request error."""
    response = integration_api_client.post(
        "/change-sets",
        json={
            "operation": "create",
            "owning_group_id": "group-1",
            "annotation": ANNOTATION,
            "reason": "Add evidence",
            **overrides,
        },
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    error = response.json()["error"]
    assert error["code"] == "request_validation_error"
    assert error["details"]
    assert set(error["details"][0]) == {"location", "message", "type"}


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("base_version", [0, -1, True, "1"])
def test_target_requires_positive_integer_version(
    integration_api_client: TestClient, operation: str, base_version: object
) -> None:
    """Target proposals reject nonpositive and coerced versions at the boundary."""
    body = {
        "operation": operation,
        "annotation_id": UNKNOWN_ID,
        "base_version": base_version,
        "reason": "Correction",
    }
    if operation == "update":
        body["patch"] = [{"op": "remove", "path": "/negation"}]
    response = integration_api_client.post("/change-sets", json=body)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_missing_target_annotation(
    integration_api_client: TestClient, operation: str
) -> None:
    """Proposals for missing targets reuse the annotation not-found envelope."""
    body = {
        "operation": operation,
        "annotation_id": UNKNOWN_ID,
        "base_version": 1,
        "reason": "Correction",
    }
    if operation == "update":
        body["patch"] = [{"op": "remove", "path": "/negation"}]
    response = integration_api_client.post("/change-sets", json=body)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["error"]["code"] == "annotation_not_found"


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ([], "request_validation_error"),
        ({"op": "remove", "path": "/negation"}, "request_validation_error"),
        (
            [{"op": "increment", "path": "/references", "value": 1}],
            "invalid_change_set",
        ),
        ([{"op": "remove", "path": "/references/99"}], "invalid_change_set"),
    ],
)
def test_invalid_update_patch(
    integration_api_client: TestClient, patch: object, code: str
) -> None:
    """Patch envelope and semantic errors return stable typed validation details."""
    target = _create_annotation(integration_api_client)
    response = integration_api_client.post(
        "/change-sets",
        json={
            "operation": "update",
            "annotation_id": target["annotation_id"],
            "base_version": 1,
            "patch": patch,
            "reason": "Correction",
        },
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == code
    assert set(response.json()["error"]["details"][0]) == {
        "location",
        "message",
        "type",
    }


def test_false_boolean_number_test_rejects_proposal_without_audit(
    integration_api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A false JSON test returns a stable error without storing a proposal or audit."""
    client = integration_api_client
    created = client.post(
        "/annotations",
        json={
            "owning_group_id": "group-1",
            "annotation": {**ANNOTATION, "negation": False},
        },
    )
    assert created.status_code == status.HTTP_201_CREATED
    target = created.json()

    response = client.post(
        "/change-sets",
        json={
            "operation": "update",
            "annotation_id": target["annotation_id"],
            "base_version": 1,
            "patch": [
                {"op": "test", "path": "/negation", "value": 0},
                {"op": "replace", "path": "/assigned_by", "value": "NEW"},
            ],
            "reason": "Conditional correction",
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    error = response.json()["error"]
    assert error["code"] == "invalid_change_set"
    assert error["details"][0]["location"] == ["patch"]
    assert error["details"][0]["type"] == "invalid_patch_operation"
    assert client.get(f"/annotations/{target['annotation_id']}").json() == target
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ChangeSetRecord)) == 0
        assert list(session.scalars(select(AuditEventRecord.action))) == [
            "annotation.created"
        ]


@pytest.mark.parametrize(
    "format_fields", [{}, {"patch_format": "application/json-patch+json"}]
)
def test_update_accepts_implicit_or_explicit_json_patch_format(
    integration_api_client: TestClient, format_fields: dict[str, object]
) -> None:
    """Update proposals accept the supported format both explicitly and by default."""
    client = integration_api_client
    target = _create_annotation(client)
    patch = [{"op": "replace", "path": "/assigned_by", "value": "NEW"}]
    response = client.post(
        "/change-sets",
        json={
            "operation": "update",
            "annotation_id": target["annotation_id"],
            "base_version": 1,
            "patch": patch,
            "reason": "Correction",
            **format_fields,
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["patch"] == patch
    preview = client.post(f"{response.headers['location']}/preview")
    assert preview.status_code == status.HTTP_200_OK
    assert preview.json()["annotation"]["assigned_by"] == "NEW"
    assert preview.json()["can_accept"] is True


@pytest.mark.parametrize(
    "patch_format", ["application/json", "application/merge-patch+json", None]
)
def test_update_rejects_unsupported_patch_format(
    integration_api_client: TestClient, patch_format: object
) -> None:
    """Only JSON Patch is accepted as an explicitly supplied update format."""
    response = integration_api_client.post(
        "/change-sets",
        json={
            "operation": "update",
            "annotation_id": UNKNOWN_ID,
            "base_version": 1,
            "patch_format": patch_format,
            "patch": [{"op": "remove", "path": "/negation"}],
            "reason": "Correction",
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    error = response.json()["error"]
    assert error["code"] == "request_validation_error"
    assert error["details"][0]["location"][-1] == "patch_format"
    assert error["details"][0]["type"] == "literal_error"


def test_invalid_candidate_can_be_previewed_but_not_accepted(
    integration_api_client: TestClient,
) -> None:
    """Raw candidate data is preserved so review can display validation issues."""
    client = integration_api_client
    proposal = _create_proposal(client, annotation={"db_object_id": None})
    path = f"/change-sets/{proposal['change_set_id']}"
    preview = client.post(f"{path}/preview")
    assert preview.status_code == status.HTTP_200_OK
    assert preview.json()["can_accept"] is False
    assert preview.json()["annotation"] is None
    assert preview.json()["validation_errors"]
    assert set(preview.json()["validation_errors"][0]) == {
        "location",
        "message",
        "type",
    }
    accepted = client.post(f"{path}/accept", json={})
    assert accepted.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert accepted.json()["error"]["code"] == "invalid_change_set"
    assert accepted.json()["error"]["details"]
    assert client.get(path).json()["state"] == "proposed"


def test_stale_acceptance_commits_stale_state(
    integration_api_client: TestClient,
) -> None:
    """Stale acceptance reports both IDs and versions and leaves a readable state."""
    client = integration_api_client
    target = _create_annotation(client)
    proposal = client.post(
        "/change-sets",
        json={
            "operation": "delete",
            "annotation_id": target["annotation_id"],
            "base_version": 1,
            "reason": "Remove",
        },
    )
    assert proposal.status_code == status.HTTP_201_CREATED
    path = proposal.headers["location"]
    changed = client.patch(
        f"/annotations/{target['annotation_id']}",
        headers={"If-Match": '"1"'},
        json={"references": ["PMID:2"]},
    )
    assert changed.status_code == status.HTTP_200_OK
    response = client.post(f"{path}/accept")
    assert response.status_code == status.HTTP_409_CONFLICT
    error = response.json()["error"]
    assert error["code"] == "stale_change_set"
    assert error["details"] == {
        "change_set_id": proposal.json()["change_set_id"],
        "annotation_id": target["annotation_id"],
        "expected_version": 1,
        "current_version": 2,
    }
    assert client.get(path).json()["state"] == "stale"


def test_duplicate_acceptance_retains_proposal(
    integration_api_client: TestClient,
) -> None:
    """Duplicate conflicts retain their established envelope and proposal state."""
    client = integration_api_client
    target = _create_annotation(client)
    proposal = _create_proposal(client)
    path = f"/change-sets/{proposal['change_set_id']}"
    preview = client.post(f"{path}/preview")
    assert preview.status_code == status.HTTP_200_OK
    assert preview.json()["duplicate_peer_ids"] == [target["annotation_id"]]
    response = client.post(f"{path}/accept")
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["error"]["code"] == "duplicate_annotation"
    assert response.json()["error"]["details"] == {
        "peer_ids": [target["annotation_id"]]
    }
    assert client.get(path).json()["state"] == "proposed"


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("reject", {}),
        ("reject", {"review_reason": " \n "}),
        ("reject", {"review_reason": None}),
        ("reject", {"review_reason": "No", "extra": True}),
        ("accept", {"review_reason": 1}),
        ("accept", {"extra": True}),
    ],
)
def test_review_body_validation(
    integration_api_client: TestClient, action: str, body: dict[str, object]
) -> None:
    """Review metadata is validated before a proposal can transition."""
    client = integration_api_client
    proposal = _create_proposal(client)
    path = f"/change-sets/{proposal['change_set_id']}"
    response = client.post(f"{path}/{action}", json=body)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"
    assert client.get(path).json()["state"] == "proposed"


@pytest.mark.parametrize(
    ("operation", "field"),
    [
        ("create", "operation"),
        ("create", "annotation"),
        ("create", "owning_group_id"),
        ("create", "reason"),
        ("update", "annotation_id"),
        ("update", "base_version"),
        ("update", "patch"),
        ("delete", "annotation_id"),
        ("delete", "base_version"),
    ],
)
def test_required_proposal_fields(
    integration_api_client: TestClient, operation: str, field: str
) -> None:
    """Each operation requires its own proposal fields before service validation."""
    body: dict[str, object] = {"operation": operation, "reason": "Correction"}
    if operation == "create":
        body.update(annotation=ANNOTATION, owning_group_id="group-1")
    else:
        body.update(annotation_id=UNKNOWN_ID, base_version=1)
        if operation == "update":
            body["patch"] = [{"op": "remove", "path": "/negation"}]
    del body[field]
    response = integration_api_client.post("/change-sets", json=body)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize(
    "overrides", [{"owning_group_id": "group-2"}, {"reason": " \n "}]
)
def test_target_metadata_validation(
    integration_api_client: TestClient, operation: str, overrides: dict[str, object]
) -> None:
    """Target proposals require reasons and cannot override annotation ownership."""
    body: dict[str, object] = {
        "operation": operation,
        "annotation_id": UNKNOWN_ID,
        "base_version": 1,
        "reason": "Correction",
        **overrides,
    }
    if operation == "update":
        body["patch"] = [{"op": "remove", "path": "/negation"}]
    response = integration_api_client.post("/change-sets", json=body)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"
