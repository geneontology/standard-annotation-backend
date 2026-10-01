"""Verify that annotation writes require an active entity catalog subject.

Also verify that catalog replacement waits for in-progress annotation writes.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from test_change_set_workflows import PROPOSER, REVIEWER
from test_entity_import_concurrency import TIMEOUT, wait_for_lock
from test_entity_import_service import active_ids, stage

from standard_annotation_backend.domain.annotations import Annotation
from standard_annotation_backend.domain.entities import UnknownDbObjectIdError
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    AnnotationVersionRecord,
    AuditEventRecord,
    EntityMembershipRecord,
)
from standard_annotation_backend.persistence.repositories.entities import (
    EntityRepository,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_service import AnnotationService
from standard_annotation_backend.services.change_set_service import ChangeSetService
from standard_annotation_backend.services.entity_import_service import (
    EntityImportService,
)

UNKNOWN_ID = "UniProtKB:P99999"
UNKNOWN_ERROR = {
    "error": {
        "code": "unknown_db_object_id",
        "message": "Annotation db_object_id is not in the active entity catalog",
        "details": [
            {
                "location": ["db_object_id"],
                "message": "Annotation db_object_id is not in the active entity catalog",
                "type": "unknown_db_object_id",
            }
        ],
    }
}


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
    validated_annotation: Annotation,
) -> None:
    """Writes and acceptances whose resulting subject is not active are rejected.

    The subject is either removed from the catalog or changed by the write to an
    identifier that was never in it. Each operation returns the unknown-subject
    error and leaves annotations, versions, audit events, and the change set's
    review state unchanged. Publishing the subject lets the same change set be
    accepted.
    """
    client = integration_api_client
    imports = EntityImportService(unit_of_work_factory)
    original = stage(imports, session_factory, validated_annotation.db_object_id)
    imports.publish(job_id=original, actor_id="supplier")
    payload = validated_annotation.model_dump(mode="json")
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
                        else {"op": "replace", "path": "/assigned_by", "value": "OTHER"}
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
        removal = stage(imports, session_factory)
        imports.publish(job_id=removal, actor_id="supplier")
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
            json={"db_object_id": UNKNOWN_ID}
            if to_unknown
            else {"assigned_by": "OTHER"},
        )
    else:
        response = client.post(f"{proposal_path}/accept")
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json() == UNKNOWN_ERROR
    assert _counts(session_factory) == before
    if proposal_path is not None:
        proposal = client.get(proposal_path).json()
        assert proposal["state"] == "proposed"
        assert proposal["reviewed_by"] is None
        # Publishing the subject makes the same proposal acceptable again.
        restored = stage(
            imports, session_factory, validated_annotation.db_object_id, UNKNOWN_ID
        )
        imports.publish(job_id=restored, actor_id="supplier")
        assert client.post(f"{proposal_path}/accept").status_code == status.HTTP_200_OK


@pytest.mark.parametrize("proposal_delete", [False, True])
def test_deletion_succeeds_after_subject_removal(
    proposal_delete: bool,
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    """Direct and proposed deletion succeed after the subject is removed.

    The deleted annotation keeps its stored data and gains a new version.
    """
    imports = EntityImportService(unit_of_work_factory)
    original = stage(imports, session_factory, validated_annotation.db_object_id)
    imports.publish(job_id=original, actor_id="supplier")
    annotations = AnnotationService(unit_of_work_factory)
    created = annotations.create(
        payload=validated_annotation.model_dump(mode="json"),
        owning_group_id="other-group",
        context=PROPOSER,
    )
    changes = ChangeSetService(unit_of_work_factory)
    proposal = (
        changes.propose_delete(
            created.annotation_id, base_version=1, reason="Remove", context=PROPOSER
        )
        if proposal_delete
        else None
    )
    removal = stage(imports, session_factory)
    imports.publish(job_id=removal, actor_id="supplier")
    if proposal is not None:
        assert changes.accept(proposal.change_set_id, context=REVIEWER).is_deleted
    else:
        annotations.delete(created.annotation_id, expected_version=1, context=PROPOSER)
    with session_factory() as session:
        record = session.get(AnnotationRecord, created.annotation_id)
        assert record is not None
        assert (record.status, record.current_version) == ("deleted", 2)
        assert (
            record.annotation_data["db_object_id"] == validated_annotation.db_object_id
        )


def test_staged_subject_is_rejected_and_exact_active_subject_is_accepted(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    validated_annotation: Annotation,
) -> None:
    """Only an exact active catalog identifier is accepted for a new annotation.

    A staged but unpublished identifier and a differently cased identifier are
    rejected, and rejected attempts write nothing.
    """
    imports = EntityImportService(unit_of_work_factory)
    job_id = stage(imports, session_factory, validated_annotation.db_object_id)
    annotations = AnnotationService(unit_of_work_factory)
    payload = validated_annotation.model_dump(mode="json")
    before = _counts(session_factory)
    with pytest.raises(UnknownDbObjectIdError):
        annotations.create(
            payload=payload, owning_group_id="other-group", context=PROPOSER
        )
    assert _counts(session_factory) == before
    imports.publish(job_id=job_id, actor_id="supplier")
    with pytest.raises(UnknownDbObjectIdError):
        annotations.create(
            payload={
                **payload,
                "db_object_id": validated_annotation.db_object_id.lower(),
            },
            owning_group_id="other-group",
            context=PROPOSER,
        )
    assert (
        annotations.create(
            payload=payload, owning_group_id="other-group", context=PROPOSER
        ).version
        == 1
    )


def test_annotation_write_and_catalog_removal_are_serialized(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    database_engine: Engine,
    validated_annotation: Annotation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog replacement waits for an in-progress annotation create to commit.

    The replacement removes the subject only after the create commits, and later
    updates to that annotation are rejected.
    """
    imports = EntityImportService(unit_of_work_factory)
    original = stage(
        imports, session_factory, validated_annotation.db_object_id, "MGI:old"
    )
    imports.publish(job_id=original, actor_id="supplier")
    replacement = stage(imports, session_factory, "MGI:new1", "MGI:new2")
    validated = Event()
    release_write = Event()
    publishing = Event()
    pids: list[int] = []
    require_active = EntityRepository.require_active

    def hold_validation(
        repository: EntityRepository, db_object_id: str
    ) -> EntityMembershipRecord:
        membership = require_active(repository, db_object_id)
        validated.set()
        assert release_write.wait(TIMEOUT)
        return membership

    def publish() -> None:
        with session_factory() as session:
            pids.append(session.scalar(text("SELECT pg_backend_pid()")))
            publishing.set()
            EntityRepository(session).publish(replacement)
            session.commit()

    monkeypatch.setattr(EntityRepository, "require_active", hold_validation)
    annotations = AnnotationService(unit_of_work_factory)
    payload = validated_annotation.model_dump(mode="json")
    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(
            annotations.create,
            payload=payload,
            owning_group_id="other-group",
            context=PROPOSER,
        )
        try:
            assert validated.wait(TIMEOUT)
            publication = pool.submit(publish)
            assert publishing.wait(TIMEOUT)
            wait_for_lock(database_engine, pids[0])
            assert active_ids(session_factory) == sorted(
                [validated_annotation.db_object_id, "MGI:old"]
            )
            assert _counts(session_factory)[0] == 0
        finally:
            release_write.set()
        created = write.result(TIMEOUT)
        publication.result(TIMEOUT)
    assert active_ids(session_factory) == ["MGI:new1", "MGI:new2"]
    assert annotations.get(created.annotation_id, context=PROPOSER).version == 1
    with pytest.raises(UnknownDbObjectIdError):
        annotations.patch(
            created.annotation_id,
            changes={"assigned_by": "OTHER"},
            expected_version=1,
            context=PROPOSER,
        )
