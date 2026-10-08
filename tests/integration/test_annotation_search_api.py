"""Test the public annotation search API against PostgreSQL data."""

from collections.abc import Callable

import pytest
from fastapi import status
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def active_search_subjects(seed_active_subjects: Callable[..., None]) -> None:
    """Seed the exact subjects used by the successful search examples."""
    seed_active_subjects(
        "UniProtKB:P00001",
        "UniProtKB:P00002",
        "UniProtKB:P00003",
        "UniProtKB:F1001",
        "UniProtKB:F1002",
        "UniProtKB:C1001",
        "UniProtKB:C1002",
        "UniProtKB:C1003",
        "UniProtKB:M1001",
        "UniProtKB:M1002",
        "UniProtKB:M1003",
        "UniProtKB:U1001",
    )


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
    integration_api_client: TestClient,
) -> None:
    """Search uses documented pagination defaults and a stable result order."""
    annotation_ids = [
        _create_annotation(integration_api_client, f"UniProtKB:P0000{index}")
        for index in range(1, 4)
    ]

    response = integration_api_client.get("/annotations")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert [item["annotation_id"] for item in body["items"]] == annotation_ids


def test_annotation_search_api_pagination_applies_limit_and_offset(
    integration_api_client: TestClient,
) -> None:
    """Search applies the requested limit and offset while retaining the total."""
    annotation_ids = [
        _create_annotation(integration_api_client, f"UniProtKB:P0000{index}")
        for index in range(1, 4)
    ]

    response = integration_api_client.get(
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
    integration_api_client: TestClient,
    params: dict[str, int],
) -> None:
    """Search rejects limits outside 1 through 200 and negative offsets."""
    response = integration_api_client.get("/annotations", params=params)

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
    integration_api_client: TestClient,
    query: dict[str, str],
    matching_changes: dict[str, object],
    excluded_changes: dict[str, object],
) -> None:
    """Each single-valued search field excludes annotations that do not match."""
    matching_id = _create_annotation(
        integration_api_client,
        "UniProtKB:F1001",
        **matching_changes,
    )
    _create_annotation(
        integration_api_client,
        "UniProtKB:F1002",
        **excluded_changes,
    )

    response = integration_api_client.get("/annotations", params=query)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 1
    assert [item["annotation_id"] for item in response.json()["items"]] == [matching_id]


@pytest.mark.parametrize(
    ("field_name", "first_value", "second_value"),
    [
        ("references", "PMID:first", "PMID:second"),
        ("with_or_from", "UniProtKB:FROM1", "UniProtKB:FROM2"),
        (
            "interacting_taxon_id",
            "NCBITaxon:9606",
            "NCBITaxon:10090",
        ),
    ],
)
def test_annotation_search_api_filter_supports_each_multivalued_field(
    integration_api_client: TestClient,
    field_name: str,
    first_value: str,
    second_value: str,
) -> None:
    """A list-valued filter finds annotations containing the value; repeating it requires all."""
    only_first_id = _create_annotation(
        integration_api_client, "UniProtKB:M1001", **{field_name: [first_value]}
    )
    _create_annotation(
        integration_api_client, "UniProtKB:M1002", **{field_name: [second_value]}
    )
    both_id = _create_annotation(
        integration_api_client,
        "UniProtKB:M1003",
        **{field_name: [first_value, second_value]},
    )

    single = integration_api_client.get(
        "/annotations", params={field_name: first_value}
    )
    repeated = integration_api_client.get(
        "/annotations",
        params=[(field_name, first_value), (field_name, second_value)],
    )

    assert single.status_code == status.HTTP_200_OK
    assert single.json()["total"] == 2
    assert [item["annotation_id"] for item in single.json()["items"]] == [
        only_first_id,
        both_id,
    ]
    assert repeated.status_code == status.HTTP_200_OK
    assert repeated.json()["total"] == 1
    assert [item["annotation_id"] for item in repeated.json()["items"]] == [both_id]


def test_annotation_search_api_filter_combines_fields_with_and(
    integration_api_client: TestClient,
) -> None:
    """Annotations must satisfy every filter supplied in one request."""
    matching_id = _create_annotation(
        integration_api_client,
        "UniProtKB:C1001",
        assigned_by="MGI",
        references=["PMID:shared"],
    )
    _create_annotation(
        integration_api_client,
        "UniProtKB:C1002",
        assigned_by="MGI",
        references=["PMID:other"],
    )
    _create_annotation(
        integration_api_client,
        "UniProtKB:C1003",
        assigned_by="GO_Central",
        references=["PMID:shared"],
    )

    response = integration_api_client.get(
        "/annotations",
        params={"assigned_by": "MGI", "references": "PMID:shared"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 1
    assert [item["annotation_id"] for item in response.json()["items"]] == [matching_id]


_UNKNOWN_PARAMETER_MESSAGE = "Query parameter is not recognized"
_UNSUPPORTED_FILTER_MESSAGE = "Annotation filter is not supported"


def _located_error(code: str, message: str, parameter: str) -> dict[str, object]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": [
                {"location": ["query", parameter], "message": message, "type": code}
            ],
        }
    }


@pytest.mark.parametrize(
    ("parameter", "expected_code", "expected_message"),
    [
        ("annotation_extensions", "unsupported_filter", _UNSUPPORTED_FILTER_MESSAGE),
        ("annotation_properties", "unsupported_filter", _UNSUPPORTED_FILTER_MESSAGE),
        ("unexpected", "unknown_query_parameter", _UNKNOWN_PARAMETER_MESSAGE),
        ("referneces", "unknown_query_parameter", _UNKNOWN_PARAMETER_MESSAGE),
        ("colour", "unknown_query_parameter", _UNKNOWN_PARAMETER_MESSAGE),
    ],
)
def test_annotation_search_api_filter_rejects_unsupported_and_unknown_names(
    integration_api_client: TestClient,
    parameter: str,
    expected_code: str,
    expected_message: str,
) -> None:
    """Search distinguishes unsupported filters from unknown parameter names.

    The response locates the offending parameter in its details and keeps the
    message fixed, so the client-supplied name never appears in the message.
    """
    _create_annotation(integration_api_client, "UniProtKB:U1001")

    response = integration_api_client.get(
        "/annotations",
        params={parameter: "red"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json() == _located_error(expected_code, expected_message, parameter)
    assert parameter not in response.json()["error"]["message"]


def test_annotation_search_api_unknown_name_precedes_known_value_validation(
    integration_api_client: TestClient,
) -> None:
    """An unknown parameter is reported before invalid known values are parsed."""
    response = integration_api_client.get(
        "/annotations",
        params={"unexpected": "x", "limit": 0},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json() == _located_error(
        "unknown_query_parameter", _UNKNOWN_PARAMETER_MESSAGE, "unexpected"
    )


def test_annotation_search_api_unsupported_name_precedes_all_other_validation(
    integration_api_client: TestClient,
) -> None:
    """An unsupported filter is reported before other query errors."""
    response = integration_api_client.get(
        "/annotations",
        params=[
            ("unexpected", "x"),
            ("annotation_extensions", "x"),
            ("limit", "0"),
        ],
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json() == _located_error(
        "unsupported_filter", _UNSUPPORTED_FILTER_MESSAGE, "annotation_extensions"
    )
