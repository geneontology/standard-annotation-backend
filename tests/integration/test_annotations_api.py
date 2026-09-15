"""Test the public annotation API against PostgreSQL data."""

from collections.abc import Callable
from uuid import UUID

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.persistence.unit_of_work import SqlAlchemyUnitOfWork

VALID_ANNOTATION = {
    "db_object_id": "UniProtKB:P12345",
    "relation": "RO:0002331",
    "ontology_class_id": "GO:0008150",
    "references": ["PMID:1"],
    "evidence_type": "ECO:0000314",
    "annotation_date": "2026-09-11",
    "assigned_by": "GO_Central",
}
NORMALIZED_VALID_ANNOTATION = Annotation.model_validate(VALID_ANNOTATION).model_dump(
    mode="json"
)
UNKNOWN_ANNOTATION_ID = UUID("00000000-0000-0000-0000-000000000501")


def test_create_and_read_annotation(integration_api_client: TestClient) -> None:
    """Creating an annotation returns identifiers, headers, and readable data."""
    response = integration_api_client.post(
        "/annotations",
        json={"owning_group_id": "group-1", "annotation": VALID_ANNOTATION},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    annotation_id = UUID(body["annotation_id"])
    assert body["version"] == 1
    assert body["annotation"] == NORMALIZED_VALID_ANNOTATION
    assert response.headers["location"] == f"/annotations/{annotation_id}"
    assert response.headers["etag"] == '"1"'

    read = integration_api_client.get(f"/annotations/{annotation_id}")
    assert read.status_code == status.HTTP_200_OK
    assert read.headers["etag"] == '"1"'
    assert read.json() == body


def test_create_validation_reports_nested_annotation_location(
    integration_api_client: TestClient,
) -> None:
    """Invalid annotation data reports its location within the request body."""
    response = integration_api_client.post(
        "/annotations",
        json={
            "owning_group_id": "group-1",
            "annotation": {**VALID_ANNOTATION, "db_object_id": None},
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    body = response.json()
    assert body["error"]["code"] == "request_validation_error"
    assert body["error"]["message"] == "Request validation failed"
    assert any(
        issue["location"][:2] == ["body", "annotation"]
        for issue in body["error"]["details"]
    )


def test_blank_ownership_validation_uses_standard_error_envelope(
    integration_api_client: TestClient,
) -> None:
    """A blank owning group returns the standard request-validation response."""
    response = integration_api_client.post(
        "/annotations",
        json={"owning_group_id": "   ", "annotation": VALID_ANNOTATION},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"
    assert response.json()["error"]["details"][0]["location"] == [
        "body",
        "owning_group_id",
    ]


def test_path_validation_uses_standard_error_envelope(
    integration_api_client: TestClient,
) -> None:
    """An invalid annotation identifier returns the standard error response."""
    response = integration_api_client.get("/annotations/not-a-uuid")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    body = response.json()
    assert body["error"]["code"] == "request_validation_error"
    assert body["error"]["message"] == "Request validation failed"
    assert body["error"]["details"][0]["location"] == ["path", "annotation_id"]
    assert "input" not in body["error"]["details"][0]


def test_read_not_found_uses_standard_error_envelope(
    integration_api_client: TestClient,
) -> None:
    """Reading an unknown annotation returns the public not-found response."""
    response = integration_api_client.get(f"/annotations/{UNKNOWN_ANNOTATION_ID}")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json() == {
        "error": {
            "code": "annotation_not_found",
            "message": "Annotation was not found",
        }
    }


def _create_annotation(
    client: TestClient,
    *,
    annotation: dict[str, object] | None = None,
) -> dict[str, object]:
    response = client.post(
        "/annotations",
        json={
            "owning_group_id": "group-1",
            "annotation": annotation or VALID_ANNOTATION,
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()


@pytest.mark.parametrize("if_match", [None, "1"])
def test_patch_rejects_missing_or_malformed_if_match(
    integration_api_client: TestClient,
    if_match: str | None,
) -> None:
    """Updates require one quoted positive version in the If-Match header."""
    created = _create_annotation(integration_api_client)
    headers = {} if if_match is None else {"If-Match": if_match}

    response = integration_api_client.patch(
        f"/annotations/{created['annotation_id']}",
        headers=headers,
        json={"assigned_by": "MGI"},
    )

    assert response.status_code == (
        status.HTTP_428_PRECONDITION_REQUIRED
        if if_match is None
        else status.HTTP_400_BAD_REQUEST
    )
    assert response.json()["error"]["code"] == (
        "precondition_required" if if_match is None else "malformed_precondition"
    )


@pytest.mark.parametrize("method", ["patch", "delete"])
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([("If-Match", '"1"'), ("If-Match", '"2"')], id="two-numeric"),
        pytest.param([("If-Match", '"1"'), ("if-match", "*")], id="numeric-wildcard"),
        pytest.param([("If-Match", '"1", "2"')], id="tag-list"),
        pytest.param([("If-Match", '"' + "9" * 4301 + '"')], id="oversized-integer"),
    ],
)
def test_mutation_rejects_malformed_preconditions_without_changing_state(
    integration_api_client: TestClient,
    method: str,
    headers: list[tuple[str, str]],
) -> None:
    """Malformed or repeated If-Match headers leave the annotation unchanged."""
    created = _create_annotation(integration_api_client)
    path = f"/annotations/{created['annotation_id']}"

    response = integration_api_client.request(
        method,
        path,
        headers=headers,
        json={"assigned_by": "MGI"} if method == "patch" else None,
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {
        "error": {
            "code": "malformed_precondition",
            "message": "If-Match must be one quoted positive integer",
        }
    }
    current = integration_api_client.get(path)
    assert current.status_code == status.HTTP_200_OK
    assert current.json() == created
    assert current.headers["etag"] == '"1"'
    history = integration_api_client.get(f"{path}/versions")
    assert history.status_code == status.HTTP_200_OK
    assert history.json()["total"] == 1
    assert [item["version"] for item in history.json()["items"]] == [1]


def test_patch_rejects_a_stale_version(integration_api_client: TestClient) -> None:
    """An update based on an outdated version reports both version numbers."""
    created = _create_annotation(integration_api_client)

    response = integration_api_client.patch(
        f"/annotations/{created['annotation_id']}",
        headers={"If-Match": '"9"'},
        json={"assigned_by": "MGI"},
    )

    assert response.status_code == status.HTTP_412_PRECONDITION_FAILED
    assert response.json() == {
        "error": {
            "code": "stale_annotation_version",
            "message": "Annotation version does not match If-Match",
            "details": {
                "annotation_id": created["annotation_id"],
                "expected_version": 9,
                "current_version": 1,
            },
        }
    }


def test_patch_replaces_a_supplied_top_level_field(
    integration_api_client: TestClient,
) -> None:
    """An updated top-level list replaces the complete previous list."""
    created = _create_annotation(integration_api_client)

    response = integration_api_client.patch(
        f"/annotations/{created['annotation_id']}",
        headers={"If-Match": '"1"'},
        json={"references": ["PMID:2"]},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["etag"] == '"2"'
    assert response.json()["version"] == 2
    assert response.json()["annotation"]["references"] == ["PMID:2"]


def test_patch_preserves_omitted_fields(integration_api_client: TestClient) -> None:
    """Fields omitted from an update retain their current values."""
    created = _create_annotation(integration_api_client)

    response = integration_api_client.patch(
        f"/annotations/{created['annotation_id']}",
        headers={"If-Match": '"1"'},
        json={"assigned_by": "MGI"},
    )

    assert response.status_code == status.HTTP_200_OK
    annotation = response.json()["annotation"]
    assert annotation["assigned_by"] == "MGI"
    assert annotation["db_object_id"] == "UniProtKB:P12345"
    assert annotation["references"] == ["PMID:1"]


def test_patch_rejects_explicit_null_for_a_required_field(
    integration_api_client: TestClient,
) -> None:
    """Setting a required annotation field to null returns a validation error."""
    created = _create_annotation(integration_api_client)

    response = integration_api_client.patch(
        f"/annotations/{created['annotation_id']}",
        headers={"If-Match": '"1"'},
        json={"relation": None},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "invalid_annotation"
    assert response.json()["error"]["details"][0]["location"] == ["relation"]


def test_patch_rejects_unknown_fields(integration_api_client: TestClient) -> None:
    """An unknown update field returns a request-validation error."""
    created = _create_annotation(integration_api_client)

    response = integration_api_client.patch(
        f"/annotations/{created['annotation_id']}",
        headers={"If-Match": '"1"'},
        json={"unexpected": True},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"
    assert response.json()["error"]["details"][0]["location"] == [
        "body",
        "unexpected",
    ]


def test_patch_rejects_an_empty_body(integration_api_client: TestClient) -> None:
    """An update request with no fields returns a specific validation error."""
    created = _create_annotation(integration_api_client)

    response = integration_api_client.patch(
        f"/annotations/{created['annotation_id']}",
        headers={"If-Match": '"1"'},
        json={},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json() == {
        "error": {
            "code": "empty_annotation_patch",
            "message": "Annotation patch must include at least one field",
        }
    }


def test_patch_rejects_a_new_duplicate(integration_api_client: TestClient) -> None:
    """An update that creates a duplicate identifies the conflicting annotation."""
    peer = _create_annotation(integration_api_client)
    distinct = _create_annotation(
        integration_api_client,
        annotation={**VALID_ANNOTATION, "db_object_id": "UniProtKB:Q99999"},
    )

    response = integration_api_client.patch(
        f"/annotations/{distinct['annotation_id']}",
        headers={"If-Match": '"1"'},
        json={"db_object_id": "UniProtKB:P12345"},
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json() == {
        "error": {
            "code": "duplicate_annotation",
            "message": "Annotation conflicts with active duplicates",
            "details": {"peer_ids": [peer["annotation_id"]]},
        }
    }


def test_patch_same_value_still_creates_a_version(
    integration_api_client: TestClient,
) -> None:
    """Supplying an unchanged value still creates a new saved version."""
    created = _create_annotation(integration_api_client)

    response = integration_api_client.patch(
        f"/annotations/{created['annotation_id']}",
        headers={"If-Match": '"1"'},
        json={"assigned_by": "GO_Central"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["etag"] == '"2"'
    assert response.json()["version"] == 2
    assert response.json()["annotation"] == NORMALIZED_VALID_ANNOTATION


def test_delete_requires_if_match(integration_api_client: TestClient) -> None:
    """Deleting an annotation requires an If-Match version header."""
    created = _create_annotation(integration_api_client)

    response = integration_api_client.delete(f"/annotations/{created['annotation_id']}")

    assert response.status_code == status.HTTP_428_PRECONDITION_REQUIRED
    assert response.json()["error"]["code"] == "precondition_required"


def test_delete_rejects_a_stale_version(integration_api_client: TestClient) -> None:
    """A deletion based on an outdated version leaves the annotation active."""
    created = _create_annotation(integration_api_client)

    response = integration_api_client.delete(
        f"/annotations/{created['annotation_id']}",
        headers={"If-Match": '"9"'},
    )

    assert response.status_code == status.HTTP_412_PRECONDITION_FAILED
    assert response.json()["error"]["code"] == "stale_annotation_version"
    current = integration_api_client.get(f"/annotations/{created['annotation_id']}")
    assert current.status_code == status.HTTP_200_OK
    assert current.json()["version"] == 1


def test_delete_commits_an_empty_response_and_hides_the_current_resource(
    integration_api_client: TestClient,
    unit_of_work_factory: Callable[[], SqlAlchemyUnitOfWork],
) -> None:
    """A successful deletion is saved before an empty HTTP 204 is returned."""
    created = _create_annotation(integration_api_client)
    annotation_id = UUID(str(created["annotation_id"]))

    response = integration_api_client.delete(
        f"/annotations/{annotation_id}",
        headers={"If-Match": '"1"'},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert response.content == b""
    current = integration_api_client.get(f"/annotations/{annotation_id}")
    assert current.status_code == status.HTTP_404_NOT_FOUND
    assert current.json()["error"]["code"] == "annotation_not_found"
    with unit_of_work_factory() as unit_of_work:
        versions = unit_of_work.annotations.list_versions(annotation_id)
        version_history = [
            (version.version, version.is_deleted, version.actor_id)
            for version in versions
        ]
    assert version_history == [(1, False, "api-test-user"), (2, True, "api-test-user")]


def test_delete_repeated_request_returns_not_found(
    integration_api_client: TestClient,
) -> None:
    """Deleting an already deleted annotation returns not found."""
    created = _create_annotation(integration_api_client)
    path = f"/annotations/{created['annotation_id']}"
    first = integration_api_client.delete(path, headers={"If-Match": '"1"'})
    assert first.status_code == status.HTTP_204_NO_CONTENT

    repeated = integration_api_client.delete(path, headers={"If-Match": '"2"'})

    assert repeated.status_code == status.HTTP_404_NOT_FOUND
    assert repeated.json()["error"]["code"] == "annotation_not_found"


def test_delete_unknown_annotation_returns_not_found(
    integration_api_client: TestClient,
) -> None:
    """Deleting an unknown annotation returns the public not-found response."""
    response = integration_api_client.delete(
        f"/annotations/{UNKNOWN_ANNOTATION_ID}",
        headers={"If-Match": '"1"'},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["error"]["code"] == "annotation_not_found"


def test_version_history_remains_accessible_after_delete(
    integration_api_client: TestClient,
) -> None:
    """Saved version history remains readable after an annotation is deleted."""
    created = _create_annotation(integration_api_client)
    annotation_id = created["annotation_id"]
    patched = integration_api_client.patch(
        f"/annotations/{annotation_id}",
        headers={"If-Match": '"1"'},
        json={"assigned_by": "MGI"},
    )
    assert patched.status_code == status.HTTP_200_OK
    deleted = integration_api_client.delete(
        f"/annotations/{annotation_id}",
        headers={"If-Match": '"2"'},
    )
    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert (
        integration_api_client.get(f"/annotations/{annotation_id}").status_code
        == status.HTTP_404_NOT_FOUND
    )

    current_collection = integration_api_client.get(
        "/annotations",
        params={"db_object_id": VALID_ANNOTATION["db_object_id"]},
    )
    assert current_collection.status_code == status.HTTP_200_OK
    assert current_collection.json()["total"] == 0

    default_page = integration_api_client.get(f"/annotations/{annotation_id}/versions")

    first_page = integration_api_client.get(
        f"/annotations/{annotation_id}/versions",
        params={"limit": 2, "offset": 0},
    )
    second_page = integration_api_client.get(
        f"/annotations/{annotation_id}/versions",
        params={"limit": 2, "offset": 2},
    )

    assert default_page.status_code == status.HTTP_200_OK
    assert default_page.json()["limit"] == 50
    assert default_page.json()["offset"] == 0
    assert [item["version"] for item in default_page.json()["items"]] == [1, 2, 3]
    assert first_page.status_code == status.HTTP_200_OK
    assert "etag" not in first_page.headers
    assert first_page.json()["total"] == 3
    assert first_page.json()["limit"] == 2
    assert first_page.json()["offset"] == 0
    assert [item["version"] for item in first_page.json()["items"]] == [1, 2]
    assert second_page.status_code == status.HTTP_200_OK
    assert second_page.json()["total"] == 3
    assert second_page.json()["limit"] == 2
    assert second_page.json()["offset"] == 2
    assert [item["version"] for item in second_page.json()["items"]] == [3]
    assert second_page.json()["items"][0]["is_deleted"] is True


def test_exact_annotation_version_returns_the_saved_snapshot_without_etag(
    integration_api_client: TestClient,
) -> None:
    """A version endpoint returns saved data without a current-resource ETag."""
    created = _create_annotation(integration_api_client)
    annotation_id = created["annotation_id"]
    patched = integration_api_client.patch(
        f"/annotations/{annotation_id}",
        headers={"If-Match": '"1"'},
        json={"assigned_by": "MGI"},
    )
    assert patched.status_code == status.HTTP_200_OK
    deleted = integration_api_client.delete(
        f"/annotations/{annotation_id}",
        headers={"If-Match": '"2"'},
    )
    assert deleted.status_code == status.HTTP_204_NO_CONTENT

    version_two = integration_api_client.get(f"/annotations/{annotation_id}/versions/2")
    deletion_version = integration_api_client.get(
        f"/annotations/{annotation_id}/versions/3"
    )

    assert version_two.status_code == status.HTTP_200_OK
    assert "etag" not in version_two.headers
    assert version_two.json()["version"] == 2
    assert version_two.json()["is_deleted"] is False
    assert version_two.json()["annotation"]["assigned_by"] == "MGI"
    assert deletion_version.status_code == status.HTTP_200_OK
    assert "etag" not in deletion_version.headers
    assert deletion_version.json()["version"] == 3
    assert deletion_version.json()["is_deleted"] is True
    assert deletion_version.json()["annotation"]["assigned_by"] == "MGI"


def test_version_routes_distinguish_unknown_annotation_and_unknown_version(
    integration_api_client: TestClient,
) -> None:
    """Missing annotations and missing versions have distinct error codes."""
    created = _create_annotation(integration_api_client)

    unknown_annotation = integration_api_client.get(
        f"/annotations/{UNKNOWN_ANNOTATION_ID}/versions"
    )
    unknown_version = integration_api_client.get(
        f"/annotations/{created['annotation_id']}/versions/99"
    )

    assert unknown_annotation.status_code == status.HTTP_404_NOT_FOUND
    assert unknown_annotation.json() == {
        "error": {
            "code": "annotation_not_found",
            "message": "Annotation was not found",
        }
    }
    assert unknown_version.status_code == status.HTTP_404_NOT_FOUND
    assert unknown_version.json() == {
        "error": {
            "code": "version_not_found",
            "message": "Annotation version was not found",
        }
    }


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
    ],
)
def test_version_history_rejects_out_of_range_pagination(
    integration_api_client: TestClient,
    params: dict[str, int],
) -> None:
    """Version history rejects limits outside 1 through 200 and negative offsets."""
    created = _create_annotation(integration_api_client)

    response = integration_api_client.get(
        f"/annotations/{created['annotation_id']}/versions",
        params=params,
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"
