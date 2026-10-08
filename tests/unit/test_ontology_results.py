"""Tests for the stored form of ontology refresh results."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from standard_annotation_backend.domain.ontology import (
    STORED_ONTOLOGY_REFRESH_RESULT,
    OntologyDocument,
    OntologyFinding,
    OntologyKey,
    OntologyRefreshResult,
    OntologyRefreshWarning,
    OntologyVersion,
)
from standard_annotation_backend.domain.stored_json import StoredDataError

_VERSION_ID = UUID("00000000-0000-0000-0000-000000000004")
_ANNOTATION_ID = UUID("00000000-0000-0000-0000-000000000005")
_FETCHED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_RESULT = OntologyRefreshResult(
    ontology_version=OntologyVersion(
        version_id=_VERSION_ID,
        ontology_key=OntologyKey.GO,
        source_type="https",
        source_locator="https://example.org/go.obo",
        source_revision=None,
        source_checksum="c" * 64,
        document_version="2026-01-01",
        loaded_predicates=("BFO:0000050",),
        fetched_at=_FETCHED,
        term_count=10,
        closure_count=20,
        active=True,
    ),
    annotation_scan_count=3,
    annotation_update_count=1,
    annotation_skip_count=1,
    findings=(
        OntologyFinding(
            code="obsolete_without_replacement",
            ontology_key=OntologyKey.GO,
            term_id="GO:0000001",
            field_paths=("ontology_class_id",),
            annotation_id=_ANNOTATION_ID,
        ),
    ),
    ontology_warnings=(
        OntologyRefreshWarning("undefined_consider_target", "GO:1", "GO:2"),
    ),
)
_LEGACY = {
    "ontology": "go",
    "ontology_version_id": str(_VERSION_ID),
    "source_type": "https",
    "source_locator": "https://example.org/go.obo",
    "source_revision": None,
    "source_checksum": "c" * 64,
    "document_version": "2026-01-01",
    "loaded_predicates": ["BFO:0000050"],
    "term_count": 10,
    "closure_count": 20,
    "annotation_scan_count": 3,
    "annotation_update_count": 1,
    "annotation_skip_count": 1,
    "ontology_warnings": [
        {
            "code": "undefined_consider_target",
            "term_id": "GO:1",
            "referenced_term_id": "GO:2",
        }
    ],
    "findings": [
        {
            "code": "obsolete_without_replacement",
            "ontology": "go",
            "term_id": "GO:0000001",
            "annotation_id": str(_ANNOTATION_ID),
            "field_paths": ["ontology_class_id"],
            "replacement_ids": [],
            "conflicting_annotation_ids": [],
        }
    ],
}


def test_job_result_keeps_its_keys() -> None:
    """The ontology job result has the same flattened keys and values as before."""
    assert _RESULT.to_job_result() == _LEGACY


def test_stored_result_loads_back() -> None:
    """A result stored by earlier code loads and writes back unchanged."""
    assert (
        STORED_ONTOLOGY_REFRESH_RESULT.dump(
            STORED_ONTOLOGY_REFRESH_RESULT.load(_LEGACY)
        )
        == _LEGACY
    )


def test_already_active_result_keeps_every_key() -> None:
    """The result for an already-active document keeps every key with empty values."""
    document = OntologyDocument(
        ontology_key=OntologyKey.GO,
        content=b"",
        source_type="https",
        source_locator="https://example.org/go.obo",
        source_revision="r1",
        source_checksum="c" * 64,
        fetched_at=_FETCHED,
    )
    assert OntologyRefreshResult.for_active_document(document).to_job_result() == {
        "ontology": "go",
        "ontology_version_id": None,
        "source_type": "https",
        "source_locator": "https://example.org/go.obo",
        "source_revision": "r1",
        "source_checksum": "c" * 64,
        "document_version": None,
        "loaded_predicates": [],
        "term_count": 0,
        "closure_count": 0,
        "annotation_scan_count": 0,
        "annotation_update_count": 0,
        "annotation_skip_count": 0,
        "ontology_warnings": [],
        "findings": [],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"annotation_update_count": None},
        {"annotation_scan_count": "3"},
        {"term_count": -1},
        {"findings": {"code": "x"}},
        {"ontology": "not-an-ontology"},
    ],
)
def test_corrupt_stored_result_is_rejected(change: dict[str, object]) -> None:
    """A stored result with a missing or invalid field is rejected instead of being read as zero."""
    with pytest.raises(StoredDataError):
        STORED_ONTOLOGY_REFRESH_RESULT.load({**_LEGACY, **change})


def test_missing_count_is_rejected() -> None:
    """A stored result without one of its counts is rejected."""
    legacy = {
        key: value for key, value in _LEGACY.items() if key != "annotation_skip_count"
    }
    with pytest.raises(StoredDataError):
        STORED_ONTOLOGY_REFRESH_RESULT.load(legacy)
