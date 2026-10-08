"""Test closure-aware annotation search and its stable public errors."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from standard_annotation_backend.domain.ontology import (
    OntologyClosureRow,
    OntologyDocument,
    OntologyKey,
    OntologySnapshot,
    OntologyTerm,
)
from standard_annotation_backend.persistence.models import JobRecord
from standard_annotation_backend.persistence.unit_of_work import UnitOfWorkFactory


@pytest.fixture(autouse=True)
def active_closure_subjects(seed_active_subjects: Callable[..., None]) -> None:
    """Seed annotation subjects used to exercise ontology filtering."""
    seed_active_subjects("UniProtKB:C1", "UniProtKB:C2", "UniProtKB:C3", "UniProtKB:C4")


def _annotation(
    object_id: str,
    term_id: str,
    *,
    assigned_by: str = "GO_Central",
) -> dict[str, object]:
    return {
        "db_object_id": object_id,
        "relation": "RO:0002331",
        "ontology_class_id": term_id,
        "references": [f"PMID:{object_id.rsplit(':', 1)[-1]}"],
        "evidence_type": "ECO:0000314",
        "annotation_date": "2026-09-28",
        "assigned_by": assigned_by,
    }


def _create(
    client: TestClient,
    object_id: str,
    term_id: str,
    *,
    assigned_by: str = "GO_Central",
) -> str:
    response = client.post(
        "/annotations",
        json={
            "owning_group_id": "group-1",
            "annotation": _annotation(
                object_id,
                term_id,
                assigned_by=assigned_by,
            ),
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    return str(response.json()["annotation_id"])


def _load_active_ontology(unit_of_work_factory: UnitOfWorkFactory) -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    job_id = UUID("00000000-0000-0000-0000-000000000071")
    document = OntologyDocument(
        ontology_key=OntologyKey.GO,
        content=b"fixture",
        source_type="test",
        source_locator="fixture/go.obo",
        source_revision="closure-api",
        source_checksum="d" * 64,
        fetched_at=now,
    )
    term_ids = ("GO:1", "GO:2", "GO:3", "GO:4")
    terms = {term_id: OntologyTerm(term_id, False, (), ()) for term_id in term_ids}
    snapshot = OntologySnapshot(
        document,
        "closure-api",
        ("rdfs:subClassOf", "BFO:0000050"),
        terms,
        (),
    )
    with unit_of_work_factory() as uow:
        job = JobRecord(
            job_id=job_id,
            job_type="ontology_refresh",
            status="queued",
            requested_by="test",
            parameters={},
            progress={},
            warnings=[],
            created_at=now,
            updated_at=now,
        )
        uow.jobs.session.add(job)
        uow.jobs.session.flush([job])
        version = uow.ontologies.stage(
            job_id=job_id,
            document=document,
            snapshot=snapshot,
            closure_rows=(
                *(
                    OntologyClosureRow(term_id, predicate, term_id, 0)
                    for predicate in snapshot.closure_predicates
                    for term_id in term_ids
                ),
                OntologyClosureRow("GO:2", "rdfs:subClassOf", "GO:1", 1),
                OntologyClosureRow("GO:3", "rdfs:subClassOf", "GO:1", 1),
                OntologyClosureRow("GO:4", "rdfs:subClassOf", "GO:1", 1),
                OntologyClosureRow("GO:4", "BFO:0000050", "GO:2", 1),
            ),
        )
        uow.ontologies.activate(version.version_id)
        uow.commit()


@pytest.mark.parametrize(
    ("params", "expected_code", "expected_message", "expected_location"),
    [
        (
            {"ontology_class_id_closure": "rdfs:subClassOf"},
            "closure_term_required",
            "ontology_class_id is required for closure search",
            ["query", "ontology_class_id"],
        ),
        (
            {"evidence_type_closure": "rdfs:subClassOf"},
            "unsupported_closure_field",
            "Closure search is supported only for ontology_class_id",
            ["query", "evidence_type_closure"],
        ),
    ],
)
def test_closure_filter_input_errors_are_located(
    integration_api_client: TestClient,
    params: dict[str, str],
    expected_code: str,
    expected_message: str,
    expected_location: list[str],
) -> None:
    """Invalid closure requests return 422 with the offending query parameter."""
    response = integration_api_client.get("/annotations", params=params)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json() == {
        "error": {
            "code": expected_code,
            "message": expected_message,
            "details": [
                {
                    "location": expected_location,
                    "message": expected_message,
                    "type": expected_code,
                }
            ],
        }
    }


def test_closure_filter_without_active_ontology_is_unavailable(
    integration_api_client: TestClient,
) -> None:
    """Closure search without an active ontology returns 503 without details."""
    response = integration_api_client.get(
        "/annotations",
        params={
            "ontology_class_id": "GO:1",
            "ontology_class_id_closure": "rdfs:subClassOf",
        },
    )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json() == {
        "error": {
            "code": "ontology_unavailable",
            "message": "The ontology required for closure search is unavailable",
        }
    }


def test_closure_filter_rejects_predicate_not_loaded_by_active_snapshot(
    integration_api_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Only closure predicates declared on the active snapshot are searchable."""
    _load_active_ontology(unit_of_work_factory)

    response = integration_api_client.get(
        "/annotations",
        params={
            "ontology_class_id": "GO:1",
            "ontology_class_id_closure": "RO:unknown",
        },
    )

    message = "Closure predicate is not loaded for the active ontology"
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json() == {
        "error": {
            "code": "unsupported_closure_predicate",
            "message": message,
            "details": [
                {
                    "location": ["query", "ontology_class_id_closure"],
                    "message": message,
                    "type": "unsupported_closure_predicate",
                }
            ],
        }
    }


def test_closure_filter_combines_descendants_with_other_filters(
    integration_api_client: TestClient,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """The public filter combines descendants, pagination, and ordinary filters."""
    direct_id = _create(
        integration_api_client,
        "UniProtKB:C1",
        "GO:1",
        assigned_by="MGI",
    )
    intermediate_id = _create(
        integration_api_client,
        "UniProtKB:C2",
        "GO:2",
        assigned_by="MGI",
    )
    other_assignee_id = _create(
        integration_api_client,
        "UniProtKB:C3",
        "GO:3",
        assigned_by="RGD",
    )
    descendant_id = _create(
        integration_api_client,
        "UniProtKB:C4",
        "GO:4",
        assigned_by="MGI",
    )
    _load_active_ontology(unit_of_work_factory)

    closure_response = integration_api_client.get(
        "/annotations",
        params={
            "ontology_class_id": "GO:1",
            "ontology_class_id_closure": "rdfs:subClassOf",
        },
    )
    combined_response = integration_api_client.get(
        "/annotations",
        params={
            "ontology_class_id": "GO:1",
            "ontology_class_id_closure": "rdfs:subClassOf",
            "assigned_by": "MGI",
            "limit": 1,
        },
    )
    isolated_response = integration_api_client.get(
        "/annotations",
        params={
            "ontology_class_id": "GO:1",
            "ontology_class_id_closure": "BFO:0000050",
        },
    )

    assert closure_response.status_code == status.HTTP_200_OK
    assert closure_response.json()["total"] == 4
    assert {item["annotation_id"] for item in closure_response.json()["items"]} == {
        direct_id,
        intermediate_id,
        other_assignee_id,
        descendant_id,
    }
    assert combined_response.status_code == status.HTTP_200_OK
    assert combined_response.json()["total"] == 3
    assert len(combined_response.json()["items"]) == 1
    assert combined_response.json()["items"][0]["annotation_id"] in {
        direct_id,
        intermediate_id,
        descendant_id,
    }
    assert isolated_response.status_code == status.HTTP_200_OK
    assert isolated_response.json()["total"] == 1
    assert [item["annotation_id"] for item in isolated_response.json()["items"]] == [
        direct_id
    ]
