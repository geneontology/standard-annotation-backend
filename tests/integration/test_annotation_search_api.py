"""Test the public annotation search API against PostgreSQL data."""

import pytest
from fastapi import status
from fastapi.testclient import TestClient


def _annotation(db_object_id: str) -> dict[str, object]:
    return {
        "db_object_id": db_object_id,
        "relation": "RO:0002331",
        "ontology_class_id": "GO:0008150",
        "references": [f"PMID:{db_object_id.rsplit(':', 1)[-1]}"],
        "evidence_type": "ECO:0000314",
        "annotation_date": "2026-09-11",
        "assigned_by": "GO_Central",
    }


def _create_annotation(
    client: TestClient,
    db_object_id: str,
    **changes: object,
) -> str:
    annotation = _annotation(db_object_id)
    annotation.update(changes)
    response = client.post(
        "/annotations",
        json={
            "owning_group_id": "group-1",
            "annotation": annotation,
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    return str(response.json()["annotation_id"])


def test_annotation_search_api_pagination_defaults_and_stable_order(
    annotation_api_client: TestClient,
) -> None:
    """Search uses documented pagination defaults and a stable result order."""
    annotation_ids = [
        _create_annotation(annotation_api_client, f"UniProtKB:P0000{index}")
        for index in range(1, 4)
    ]

    response = annotation_api_client.get("/annotations")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert [item["annotation_id"] for item in body["items"]] == annotation_ids


def test_annotation_search_api_pagination_applies_limit_and_offset(
    annotation_api_client: TestClient,
) -> None:
    """Search applies the requested limit and offset while retaining the total."""
    annotation_ids = [
        _create_annotation(annotation_api_client, f"UniProtKB:P0000{index}")
        for index in range(1, 4)
    ]

    response = annotation_api_client.get(
        "/annotations",
        params={"limit": 1, "offset": 1},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 3
    assert response.json()["limit"] == 1
    assert response.json()["offset"] == 1
    assert [item["annotation_id"] for item in response.json()["items"]] == [
        annotation_ids[1]
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
    ],
)
def test_annotation_search_api_pagination_rejects_out_of_range_values(
    annotation_api_client: TestClient,
    params: dict[str, int],
) -> None:
    """Search rejects limits outside 1 through 200 and negative offsets."""
    response = annotation_api_client.get("/annotations", params=params)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"


@pytest.mark.parametrize(
    ("query", "matching_changes", "excluded_changes"),
    [
        (
            {"db_object_id": "UniProtKB:F1001"},
            {},
            {},
        ),
        (
            {"negation": "true"},
            {"negation": True},
            {"negation": False},
        ),
        (
            {"relation": "RO:0002331"},
            {"relation": "RO:0002331"},
            {"relation": "RO:0002327"},
        ),
        (
            {"ontology_class_id": "GO:0008150"},
            {"ontology_class_id": "GO:0008150"},
            {"ontology_class_id": "GO:0003674"},
        ),
        (
            {"evidence_type": "ECO:0000314"},
            {"evidence_type": "ECO:0000314"},
            {"evidence_type": "ECO:0000250"},
        ),
        (
            {"annotation_date": "2026-09-11"},
            {"annotation_date": "2026-09-11"},
            {"annotation_date": "2026-09-10"},
        ),
        (
            {"assigned_by": "GO_Central"},
            {"assigned_by": "GO_Central"},
            {"assigned_by": "MGI"},
        ),
    ],
)
def test_annotation_search_api_filter_supports_each_scalar_field(
    annotation_api_client: TestClient,
    query: dict[str, str],
    matching_changes: dict[str, object],
    excluded_changes: dict[str, object],
) -> None:
    """Each single-valued search field excludes annotations that do not match."""
    matching_id = _create_annotation(
        annotation_api_client,
        "UniProtKB:F1001",
        **matching_changes,
    )
    _create_annotation(
        annotation_api_client,
        "UniProtKB:F1002",
        **excluded_changes,
    )

    response = annotation_api_client.get("/annotations", params=query)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 1
    assert [item["annotation_id"] for item in response.json()["items"]] == [matching_id]


@pytest.mark.parametrize(
    ("field_name", "matching_value", "excluded_value"),
    [
        ("references", "PMID:search", "PMID:excluded"),
        ("with_or_from", "UniProtKB:FROM1", "UniProtKB:FROM2"),
        (
            "interacting_taxon_id",
            "NCBITaxon:9606",
            "NCBITaxon:10090",
        ),
    ],
)
def test_annotation_search_api_filter_supports_each_multivalued_field(
    annotation_api_client: TestClient,
    field_name: str,
    matching_value: str,
    excluded_value: str,
) -> None:
    """Each list-valued search field finds annotations containing the value."""
    matching_id = _create_annotation(
        annotation_api_client,
        "UniProtKB:M1001",
        **{field_name: [matching_value]},
    )
    _create_annotation(
        annotation_api_client,
        "UniProtKB:M1002",
        **{field_name: [excluded_value]},
    )

    response = annotation_api_client.get(
        "/annotations",
        params={field_name: matching_value},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 1
    assert [item["annotation_id"] for item in response.json()["items"]] == [matching_id]


def test_annotation_search_api_filter_repeated_references_require_all_values(
    annotation_api_client: TestClient,
) -> None:
    """Repeated reference filters require every requested reference."""
    matching_id = _create_annotation(
        annotation_api_client,
        "UniProtKB:R1001",
        references=["PMID:one", "PMID:two"],
    )
    _create_annotation(
        annotation_api_client,
        "UniProtKB:R1002",
        references=["PMID:one"],
    )
    _create_annotation(
        annotation_api_client,
        "UniProtKB:R1003",
        references=["PMID:two"],
    )

    response = annotation_api_client.get(
        "/annotations",
        params=[("references", "PMID:one"), ("references", "PMID:two")],
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 1
    assert [item["annotation_id"] for item in response.json()["items"]] == [matching_id]


def test_annotation_search_api_filter_combines_fields_with_and(
    annotation_api_client: TestClient,
) -> None:
    """Annotations must satisfy every filter supplied in one request."""
    matching_id = _create_annotation(
        annotation_api_client,
        "UniProtKB:C1001",
        assigned_by="MGI",
        references=["PMID:shared"],
    )
    _create_annotation(
        annotation_api_client,
        "UniProtKB:C1002",
        assigned_by="MGI",
        references=["PMID:other"],
    )
    _create_annotation(
        annotation_api_client,
        "UniProtKB:C1003",
        assigned_by="GO_Central",
        references=["PMID:shared"],
    )

    response = annotation_api_client.get(
        "/annotations",
        params={"assigned_by": "MGI", "references": "PMID:shared"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 1
    assert [item["annotation_id"] for item in response.json()["items"]] == [matching_id]


@pytest.mark.parametrize(
    ("parameter", "expected_code", "expected_message"),
    [
        (
            "annotation_extensions",
            "unsupported_filter",
            "Unsupported annotation filter: annotation_extensions",
        ),
        (
            "annotation_properties",
            "unsupported_filter",
            "Unsupported annotation filter: annotation_properties",
        ),
        (
            "unexpected",
            "unknown_query_parameter",
            "Unknown query parameter: unexpected",
        ),
        (
            "referneces",
            "unknown_query_parameter",
            "Unknown query parameter: referneces",
        ),
    ],
)
def test_annotation_search_api_filter_rejects_unsupported_and_unknown_names(
    annotation_api_client: TestClient,
    parameter: str,
    expected_code: str,
    expected_message: str,
) -> None:
    """Search distinguishes unsupported filters from unknown parameter names."""
    _create_annotation(annotation_api_client, "UniProtKB:U1001")

    response = annotation_api_client.get(
        "/annotations",
        params={parameter: "value"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {
        "error": {
            "code": expected_code,
            "message": expected_message,
        }
    }


def test_annotation_search_api_unknown_name_precedes_known_value_validation(
    annotation_api_client: TestClient,
) -> None:
    """An unknown parameter is reported before invalid known values are parsed."""
    response = annotation_api_client.get(
        "/annotations",
        params={"unexpected": "x", "limit": 0},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {
        "error": {
            "code": "unknown_query_parameter",
            "message": "Unknown query parameter: unexpected",
        }
    }


def test_annotation_search_api_unsupported_name_precedes_all_other_validation(
    annotation_api_client: TestClient,
) -> None:
    """An unsupported filter is reported before other query errors."""
    response = annotation_api_client.get(
        "/annotations",
        params=[
            ("unexpected", "x"),
            ("annotation_extensions", "x"),
            ("limit", "0"),
        ],
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == {
        "error": {
            "code": "unsupported_filter",
            "message": "Unsupported annotation filter: annotation_extensions",
        }
    }


def test_annotation_search_api_known_invalid_value_remains_validation_error(
    annotation_api_client: TestClient,
) -> None:
    """A recognized parameter with an invalid value returns a validation error."""
    response = annotation_api_client.get(
        "/annotations",
        params={"limit": 0},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["error"]["code"] == "request_validation_error"
