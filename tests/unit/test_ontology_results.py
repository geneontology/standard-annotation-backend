"""Tests for the stored form of ontology refresh results."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from standard_annotation_backend.domain.ontology import (
    GO_ONTOLOGY_KEY,
    STORED_ONTOLOGY_REFRESH_RESULT,
    OntologyDocument,
    OntologyRefreshResult,
    OntologyRefreshWarning,
    OntologyTerm,
    OntologyVersion,
    ontology_refresh_warnings,
)
from standard_annotation_backend.domain.stored_json import StoredDataError

_VERSION_ID = UUID("00000000-0000-0000-0000-000000000004")
_FETCHED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_RESULT = OntologyRefreshResult(
    ontology_version=OntologyVersion(
        version_id=_VERSION_ID,
        ontology_key=GO_ONTOLOGY_KEY,
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
    ontology_warnings=(
        OntologyRefreshWarning("undefined_consider_target", "GO:1", "GO:2"),
    ),
)
_JOB_RESULT = {
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
    "ontology_warnings": [
        {
            "code": "undefined_consider_target",
            "term_id": "GO:1",
            "referenced_term_id": "GO:2",
        }
    ],
}


def test_job_result_keeps_its_keys() -> None:
    """An ontology refresh result serializes to the flattened job-result keys clients read."""
    assert _RESULT.to_job_result() == _JOB_RESULT


def test_already_active_result_keeps_every_key() -> None:
    """The result for an already-active document keeps every key with empty values."""
    document = OntologyDocument(
        ontology_key=GO_ONTOLOGY_KEY,
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
        "ontology_warnings": [],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"term_count": None},
        {"closure_count": "3"},
        {"term_count": -1},
        {"ontology_warnings": {"code": "x"}},
        {"ontology": 5},
    ],
)
def test_corrupt_stored_result_is_rejected(change: dict[str, object]) -> None:
    """A stored result with a missing or invalid field is rejected instead of being read as zero."""
    with pytest.raises(StoredDataError):
        STORED_ONTOLOGY_REFRESH_RESULT.load({**_JOB_RESULT, **change})


def test_missing_count_is_rejected() -> None:
    """A stored result without one of its counts is rejected."""
    incomplete = {
        key: value for key, value in _JOB_RESULT.items() if key != "term_count"
    }
    with pytest.raises(StoredDataError):
        STORED_ONTOLOGY_REFRESH_RESULT.load(incomplete)


def test_consider_suggestions_into_other_ontologies_are_not_warned_about() -> None:
    """Only a missing `consider` target with the ontology's own prefix is a warning."""
    terms = {
        "GO:0000001": OntologyTerm(
            "GO:0000001", True, (), ("GO:9999999", "UBERON:0000001")
        )
    }

    warnings = ontology_refresh_warnings(terms, ("GO",))

    assert [warning.referenced_term_id for warning in warnings] == ["GO:9999999"]
