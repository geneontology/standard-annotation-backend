"""Verify that annotation writes require an active entity catalog subject."""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from refresh_helpers import TEST_ONTOLOGIES, TEST_SOURCES
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from test_change_set_workflows import PROPOSER, REVIEWER
from test_entity_refresh_service import publish_catalog, stage_catalog

from standard_annotation_backend.domain.annotations import (
    Annotation,
    InvalidAnnotationPayloadError,
)
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationVersionRecord,
    AuditEventRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_service import AnnotationService
from standard_annotation_backend.services.change_set_service import ChangeSetService
from standard_annotation_backend.services.entity_refresh_service import (
    EntityRefreshService,
)

UNKNOWN_ID = "UniProtKB:P99999"
UNKNOWN_ISSUE = {
    "location": ["annotation", "db_object_id"],
    "message": "db_object_id is not an active entity in any entity catalog",
    "type": "unknown_entity",
}

pytestmark = pytest.mark.usefixtures("reference_data")


def _counts(session_factory: sessionmaker[Session]) -> tuple[int, ...]:
    """Count current annotations, historical versions, and audit records."""
    with session_factory() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model)) or 0
            for model in (AnnotationRecord, AnnotationVersionRecord, AuditEventRecord)
        )


@pytest.mark.parametrize(
    "operation",
    [
        "create",
        "patch",
        "patch_to_unknown",
        "accept_create",
        "accept_update",
        "accept_update_to_unknown",
    ],
)
def test_unknown_subject_returns_exact_error_without_writes(
    operation: str,
    integration_api_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    writable_annotation: Annotation,
) -> None:
    """Writes and acceptances whose resulting subject is not active are rejected.

    The subject is either removed from the catalog or changed by the write to an
    identifier that was never in it. Each operation returns only the
    `unknown_entity` issue and leaves annotations, versions, audit events, and the
    change set's review state unchanged. Publishing the subject lets the same
    change set be accepted.
    """
    client = integration_api_client
    imports = EntityRefreshService(unit_of_work_factory, TEST_SOURCES)
    publish_catalog(imports, unit_of_work_factory, writable_annotation.db_object_id)
    payload = writable_annotation.model_dump(mode="json")
    to_unknown = operation.endswith("_to_unknown")
    annotation_id = None
    if operation != "create" and operation != "accept_create":
        created = client.post(
            "/annotations",
            json={"owning_group_id": "other-group", "annotation": payload},
        )
        assert created.status_code == status.HTTP_201_CREATED
        annotation_id = created.json()["annotation_id"]
    proposal_path = None
    if operation.startswith("accept"):
        proposal = client.post(
            "/change-sets",
            json=(
                {
                    "operation": "create",
                    "owning_group_id": "other-group",
                    "annotation": payload,
                    "reason": "Review",
                }
                if operation == "accept_create"
                else {
                    "operation": "update",
                    "annotation_id": annotation_id,
                    "base_version": 1,
                    "patch": [
                        {"op": "replace", "path": "/db_object_id", "value": UNKNOWN_ID}
                        if to_unknown
                        else {"op": "replace", "path": "/assigned_by", "value": "RGD"}
                    ],
                    "reason": "Review",
                }
            ),
        )
        assert proposal.status_code == status.HTTP_201_CREATED
        proposal_path = f"/change-sets/{proposal.json()['change_set_id']}"
        if not to_unknown:
            assert client.post(f"{proposal_path}/preview").json()["can_accept"]
    if not to_unknown:
        publish_catalog(imports, unit_of_work_factory)
    before = _counts(session_factory)
    if operation == "create":
        response = client.post(
            "/annotations",
            json={"owning_group_id": "other-group", "annotation": payload},
        )
    elif operation.startswith("patch"):
        response = client.patch(
            f"/annotations/{annotation_id}",
            headers={"If-Match": '"1"'},
            json={"db_object_id": UNKNOWN_ID} if to_unknown else {"assigned_by": "RGD"},
        )
    else:
        response = client.post(f"{proposal_path}/accept")
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    error = response.json()["error"]
    expected_code = (
        "invalid_change_set" if operation.startswith("accept") else "invalid_annotation"
    )
    assert error["code"] == expected_code
    assert error["details"] == [UNKNOWN_ISSUE]
    assert _counts(session_factory) == before
    if proposal_path is not None:
        proposal = client.get(proposal_path).json()
        assert proposal["state"] == "proposed"
        assert proposal["reviewed_by"] is None
        # Publishing the subject makes the same proposal acceptable again.
        publish_catalog(
            imports, unit_of_work_factory, writable_annotation.db_object_id, UNKNOWN_ID
        )
        assert client.post(f"{proposal_path}/accept").status_code == status.HTTP_200_OK


@pytest.mark.parametrize("proposal_delete", [False, True])
def test_deletion_succeeds_after_subject_removal(
    proposal_delete: bool,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    writable_annotation: Annotation,
) -> None:
    """Direct and proposed deletion succeed after the subject is removed.

    The deleted annotation keeps its stored data and gains a new version.
    """
    imports = EntityRefreshService(unit_of_work_factory, TEST_SOURCES)
    publish_catalog(imports, unit_of_work_factory, writable_annotation.db_object_id)
    annotations = AnnotationService(unit_of_work_factory, TEST_ONTOLOGIES)
    created = annotations.create(
        payload=writable_annotation.model_dump(mode="json"),
        owning_group_id="other-group",
        context=PROPOSER,
    )
    changes = ChangeSetService(unit_of_work_factory, TEST_ONTOLOGIES)
    proposal = (
        changes.propose_delete(
            created.annotation_id, base_version=1, reason="Remove", context=PROPOSER
        )
        if proposal_delete
        else None
    )
    publish_catalog(imports, unit_of_work_factory)
    if proposal is not None:
        assert changes.accept(proposal.change_set_id, context=REVIEWER).is_deleted
    else:
        annotations.delete(created.annotation_id, expected_version=1, context=PROPOSER)
    with session_factory() as session:
        record = session.get(AnnotationRecord, created.annotation_id)
        assert record is not None
        assert (record.status, record.current_version) == ("deleted", 2)
        assert (
            record.annotation_data["db_object_id"] == writable_annotation.db_object_id
        )


def test_staged_subject_is_rejected_and_exact_active_subject_is_accepted(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    writable_annotation: Annotation,
) -> None:
    """Only an exact active catalog identifier is accepted for a new annotation.

    A staged but unpublished identifier and a differently cased identifier are
    rejected, and rejected attempts write nothing.
    """
    imports = EntityRefreshService(unit_of_work_factory, TEST_SOURCES)
    job = stage_catalog(imports, unit_of_work_factory, writable_annotation.db_object_id)
    annotations = AnnotationService(unit_of_work_factory, TEST_ONTOLOGIES)
    payload = writable_annotation.model_dump(mode="json")
    before = _counts(session_factory)
    with pytest.raises(InvalidAnnotationPayloadError) as staged:
        annotations.create(
            payload=payload, owning_group_id="other-group", context=PROPOSER
        )
    assert [issue["type"] for issue in staged.value.errors] == ["unknown_entity"]
    assert _counts(session_factory) == before
    assert imports.recover(job) is not None
    with pytest.raises(InvalidAnnotationPayloadError) as cased:
        annotations.create(
            payload={
                **payload,
                "db_object_id": "UniProtKB:"
                + writable_annotation.db_object_id.partition(":")[2].lower(),
            },
            owning_group_id="other-group",
            context=PROPOSER,
        )
    assert [issue["type"] for issue in cased.value.errors] == ["unknown_entity"]
    assert (
        annotations.create(
            payload=payload, owning_group_id="other-group", context=PROPOSER
        ).version
        == 1
    )
