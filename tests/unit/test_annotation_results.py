"""Tests for the stored forms of GPAD refresh results and rejection reports."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from standard_annotation_backend.domain.annotation_management import (
    STORED_REJECTION_REPORT,
    AnnotationManagementMode,
    AnnotationRefreshResult,
    AnnotationRejection,
    RejectionReport,
)
from standard_annotation_backend.domain.refresh import SourceProvenance
from standard_annotation_backend.domain.stored_json import StoredDataError

_REPORT = RejectionReport(
    issue_count=2,
    by_code={"syntax": 1, "validation": 1},
    issues=(
        AnnotationRejection(3, "syntax", "bad line"),
        AnnotationRejection(9, "validation", "evidence_type is not allowed"),
    ),
)
_REPORT_JSON = {
    "issue_count": 2,
    "by_code": {"syntax": 1, "validation": 1},
    "issues": [
        {"line_number": 3, "code": "syntax", "reason": "bad line"},
        {
            "line_number": 9,
            "code": "validation",
            "reason": "evidence_type is not allowed",
        },
    ],
}


@pytest.mark.parametrize(
    "change",
    [
        {"issue_count": -1},
        {"issue_count": True},
        {"by_code": {"syntax": -1}},
        {"by_code": {"syntax": "1"}},
        {"issues": [{"line_number": 3, "code": "syntax"}]},
        {"issues": [{"line_number": -3, "code": "syntax", "reason": "r"}]},
        {"issues": "none"},
    ],
)
def test_corrupt_stored_rejection_report_is_rejected(change: dict[str, object]) -> None:
    """Negative or non-integer counts and malformed issues raise `StoredDataError`."""
    with pytest.raises(StoredDataError):
        STORED_REJECTION_REPORT.load({**_REPORT_JSON, **change})


def test_refresh_job_result_keeps_its_keys() -> None:
    """A published import's job result lists every field at the top level.

    Provenance is flattened, and the result includes the schema version the rows
    were checked with and the rejection report.
    """
    fetched = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    job_id = UUID("00000000-0000-0000-0000-000000000003")
    result = AnnotationRefreshResult(
        source_key="mgi",
        group_key="MGI",
        import_job_id=job_id,
        is_cutover=False,
        provenance=SourceProvenance(
            "https", "https://example.org/a.gpad", None, "b" * 64, fetched
        ),
        schema_version="0.1.0",
        data_rows=4,
        annotations_published=2,
        records_rejected=2,
        annotations_deleted=1,
        mode=AnnotationManagementMode.GPAD_IMPORTED,
        rejection_report=_REPORT,
    )
    assert result.to_job_result() == {
        "source_key": "mgi",
        "group_key": "MGI",
        "import_job_id": str(job_id),
        "is_cutover": False,
        "source_type": "https",
        "source_locator": "https://example.org/a.gpad",
        "source_revision": None,
        "source_checksum": "b" * 64,
        "fetched_at": "2026-01-02T03:04:05Z",
        "schema_version": "0.1.0",
        "data_rows": 4,
        "annotations_published": 2,
        "records_rejected": 2,
        "annotations_deleted": 1,
        "mode": "gpad_imported",
        "rejection_report": _REPORT_JSON,
    }
