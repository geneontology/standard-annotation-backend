"""Verify that every create and update must pass semantic validation."""

from collections.abc import Callable, Mapping

import httpx2
import pytest
from fastapi import status
from fastapi.testclient import TestClient
from refresh_helpers import TEST_ONTOLOGIES
from seeding import (
    DEFAULT_RULE_DATA,
    SeedTerm,
    create_job,
    insert_annotation,
    set_rule_data,
)
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_change_set_workflows import PROPOSER

from standard_annotation_backend.domain.annotations import (
    Annotation,
    AnnotationOrigin,
    InvalidAnnotationPayloadError,
)
from standard_annotation_backend.domain.jobs import JobType
from standard_annotation_backend.persistence.models import (
    AnnotationRecord,
    OntologyMetadataRecord,
    RulesSnapshotRecord,
)
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory
from standard_annotation_backend.services.annotation_service import AnnotationService

PAYLOAD = {
    "db_object_id": "UniProtKB:P12345",
    "relation": "RO:0002331",
    "ontology_class_id": "GO:0006915",
    "references": ["PMID:1"],
    "evidence_type": "ECO:0000314",
    "annotation_date": "2026-11-11",
    "assigned_by": "GO_Central",
}


def _create(client: TestClient, payload: Mapping[str, object]) -> httpx2.Response:
    return client.post(
        "/annotations",
        json={"owning_group_id": "other-group", "annotation": dict(payload)},
    )


def test_every_semantic_issue_is_returned_in_one_invalid_annotation_error(
    integration_api_client: TestClient,
    active_annotation_subjects: None,
    reference_data: Callable[..., None],
    session_factory: sessionmaker[Session],
) -> None:
    """A create with several problems lists all of them and writes nothing."""
    reference_data(SeedTerm("GO:0000003", obsolete=True, replaced_by=("GO:0008150",)))
    response = _create(
        integration_api_client,
        {
            **PAYLOAD,
            "db_object_id": "UniProtKB:UNKNOWN",
            "ontology_class_id": "GO:0000003",
            "annotation_extensions": [
                {"extension_relation": "BFO:0000066", "extension_term": "CL:9999999"}
            ],
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    error = response.json()["error"]
    assert error["code"] == "invalid_annotation"
    assert [(issue["location"], issue["type"]) for issue in error["details"]] == [
        (["annotation", "db_object_id"], "unknown_entity"),
        (
            ["annotation", "annotation_extensions", 0, "extension_term"],
            "unknown_term",
        ),
        (["annotation", "ontology_class_id"], "obsolete_term"),
    ]
    assert error["details"][2]["details"] == {
        "replaced_by": ["GO:0008150"],
        "consider": [],
    }
    assert "details" not in error["details"][0]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AnnotationRecord)) == 0


def test_a_duplicate_is_an_issue_that_lists_its_peers(
    integration_api_client: TestClient, active_annotation_subjects: None
) -> None:
    """Creating an annotation that duplicates an active one fails with its peer."""
    first = _create(integration_api_client, PAYLOAD)
    assert first.status_code == status.HTTP_201_CREATED

    second = _create(integration_api_client, PAYLOAD)

    assert second.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert second.json()["error"]["details"] == [
        {
            "location": ["annotation"],
            "message": "Annotation duplicates other active annotations",
            "type": "duplicate_annotation",
            "details": {"peer_ids": [first.json()["annotation_id"]]},
        }
    ]


def test_an_imported_duplicate_cannot_be_updated_until_the_pair_is_resolved(
    unit_of_work_factory: UnitOfWorkFactory,
    session_factory: sessionmaker[Session],
    active_annotation_subjects: None,
) -> None:
    """An update to one of two imported duplicates is rejected.

    Once the other annotation is deleted, the same update succeeds.
    """
    annotation = Annotation.model_validate(PAYLOAD)
    job = create_job(
        unit_of_work_factory,
        job_type=JobType.ANNOTATION_REFRESH,
        parameters={"source_key": "mgi"},
    )
    with session_factory() as session:
        first, second = (
            insert_annotation(
                session,
                annotation,
                record_origin=AnnotationOrigin.IMPORT,
                source_import_job_id=job.job_id,
            ).annotation_id
            for _ in range(2)
        )
        session.commit()
    service = AnnotationService(unit_of_work_factory, TEST_ONTOLOGIES)
    context = PROPOSER

    with pytest.raises(InvalidAnnotationPayloadError) as raised:
        service.patch(
            first, changes={"assigned_by": "MGI"}, expected_version=1, context=context
        )
    assert [issue["type"] for issue in raised.value.errors] == ["duplicate_annotation"]
    assert raised.value.errors[0].get("details") == {"peer_ids": [str(second)]}

    service.delete(second, expected_version=1, context=context)
    updated = service.patch(
        first, changes={"assigned_by": "MGI"}, expected_version=1, context=context
    )
    assert updated.version == 2


@pytest.mark.parametrize("missing", ["ontology", "rules"])
def test_writes_need_loaded_reference_data_but_reads_and_deletes_do_not(
    integration_api_client: TestClient,
    active_annotation_subjects: None,
    session_factory: sessionmaker[Session],
    missing: str,
) -> None:
    """Without an active snapshot, creates and updates return 503.

    The error code is `reference_data_unavailable`. Reading and deleting an
    annotation do not need reference data and still succeed.
    """
    created = _create(integration_api_client, PAYLOAD)
    annotation_id = created.json()["annotation_id"]
    with session_factory.begin() as session:
        if missing == "rules":
            session.execute(update(RulesSnapshotRecord).values(active=False))
        else:
            session.execute(
                update(OntologyMetadataRecord)
                .where(OntologyMetadataRecord.ontology_key == "chebi")
                .values(active=False)
            )

    create = _create(integration_api_client, {**PAYLOAD, "references": ["PMID:2"]})
    patch = integration_api_client.patch(
        f"/annotations/{annotation_id}",
        json={"assigned_by": "MGI"},
        headers={"If-Match": '"1"'},
    )
    read = integration_api_client.get(f"/annotations/{annotation_id}")
    delete = integration_api_client.delete(
        f"/annotations/{annotation_id}", headers={"If-Match": '"1"'}
    )

    assert (
        create.status_code == patch.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    )
    assert create.json()["error"]["code"] == "reference_data_unavailable"
    assert patch.json()["error"]["code"] == "reference_data_unavailable"
    assert read.status_code == status.HTTP_200_OK
    assert delete.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.parametrize("missing", ["ontology", "rules"])
def test_change_sets_need_loaded_reference_data_except_for_deletes(
    integration_api_client: TestClient,
    active_annotation_subjects: None,
    session_factory: sessionmaker[Session],
    missing: str,
) -> None:
    """Without an active snapshot, create previews and acceptance return 503.

    A delete proposal is never semantically validated, so it is previewed and
    accepted as usual.
    """
    client = integration_api_client
    created = _create(client, PAYLOAD)
    create_proposal = client.post(
        "/change-sets",
        json={
            "operation": "create",
            "owning_group_id": "other-group",
            "annotation": {**PAYLOAD, "references": ["PMID:2"]},
            "reason": "Review",
        },
    )
    delete_proposal = client.post(
        "/change-sets",
        json={
            "operation": "delete",
            "annotation_id": created.json()["annotation_id"],
            "base_version": 1,
            "reason": "Remove",
        },
    )
    create_path = f"/change-sets/{create_proposal.json()['change_set_id']}"
    delete_path = f"/change-sets/{delete_proposal.json()['change_set_id']}"
    with session_factory.begin() as session:
        if missing == "rules":
            session.execute(update(RulesSnapshotRecord).values(active=False))
        else:
            session.execute(
                update(OntologyMetadataRecord)
                .where(OntologyMetadataRecord.ontology_key == "chebi")
                .values(active=False)
            )

    for response in (
        client.post(f"{create_path}/preview"),
        client.post(f"{create_path}/accept"),
    ):
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"]["code"] == "reference_data_unavailable"
    assert client.get(create_path).json()["state"] == "proposed"
    assert client.post(f"{delete_path}/preview").json()["can_accept"] is True
    assert client.post(f"{delete_path}/accept").status_code == status.HTTP_200_OK


def test_preview_and_acceptance_report_the_same_semantic_issues(
    integration_api_client: TestClient, active_annotation_subjects: None
) -> None:
    """A preview lists semantic issues and cannot accept; acceptance agrees."""
    proposal = integration_api_client.post(
        "/change-sets",
        json={
            "operation": "create",
            "owning_group_id": "other-group",
            "annotation": {**PAYLOAD, "ontology_class_id": "GO:7777777"},
            "reason": "Review",
        },
    )
    path = f"/change-sets/{proposal.json()['change_set_id']}"

    preview = integration_api_client.post(f"{path}/preview").json()
    accept = integration_api_client.post(f"{path}/accept")

    expected = [(["annotation", "ontology_class_id"], "unknown_term")]
    assert preview["can_accept"] is False
    assert [
        (issue["location"], issue["type"]) for issue in preview["validation_errors"]
    ] == expected
    assert accept.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert accept.json()["error"]["code"] == "invalid_change_set"
    assert [
        (issue["location"], issue["type"])
        for issue in accept.json()["error"]["details"]
    ] == expected


def test_go_rule_issues_follow_catalog_issues_in_rule_order(
    integration_api_client: TestClient,
    active_annotation_subjects: None,
    session_factory: sessionmaker[Session],
) -> None:
    """A create that breaks several GO Rules lists each, after its catalog issues."""
    set_rule_data(session_factory, DEFAULT_RULE_DATA, retracted_pmids=("PMID:9",))

    response = _create(
        integration_api_client,
        {
            **PAYLOAD,
            "db_object_id": "UniProtKB:UNKNOWN",
            "negation": True,
            "references": ["PMID:9"],
            "annotation_extensions": [
                {"extension_relation": "BFO:0000050", "extension_term": "CL:0000000"}
            ],
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert [
        (issue["location"], issue["type"])
        for issue in response.json()["error"]["details"]
    ] == [
        (["annotation", "db_object_id"], "unknown_entity"),
        (["annotation", "references", 0], "gorule_0000022"),
        (["annotation"], "gorule_0000053"),
    ]


@pytest.mark.parametrize("missing", ["ontology", "rules"])
def test_structural_issues_are_reported_without_loaded_reference_data(
    integration_api_client: TestClient,
    active_annotation_subjects: None,
    session_factory: sessionmaker[Session],
    missing: str,
) -> None:
    """Structural issues need no reference data, so they are reported, not a 503.

    A patch that clears a required field gets `invalid_annotation`, and a
    preview of a proposal whose patch removes a required field lists that
    field's issue.
    """
    client = integration_api_client
    created = _create(client, PAYLOAD)
    proposal = client.post(
        "/change-sets",
        json={
            "operation": "update",
            "annotation_id": created.json()["annotation_id"],
            "base_version": 1,
            "patch": [{"op": "remove", "path": "/db_object_id"}],
            "reason": "Review",
        },
    )
    with session_factory.begin() as session:
        if missing == "rules":
            session.execute(update(RulesSnapshotRecord).values(active=False))
        else:
            session.execute(
                update(OntologyMetadataRecord)
                .where(OntologyMetadataRecord.ontology_key == "chebi")
                .values(active=False)
            )

    patch = client.patch(
        f"/annotations/{created.json()['annotation_id']}",
        json={"relation": None},
        headers={"If-Match": '"1"'},
    )
    preview = client.post(f"/change-sets/{proposal.json()['change_set_id']}/preview")

    assert patch.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert patch.json()["error"]["code"] == "invalid_annotation"
    assert patch.json()["error"]["details"][0]["location"] == ["annotation", "relation"]
    assert preview.status_code == status.HTTP_200_OK
    assert [issue["location"] for issue in preview.json()["validation_errors"]] == [
        ["annotation", "db_object_id"]
    ]
