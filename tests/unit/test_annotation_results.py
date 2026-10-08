"""Tests for the stored forms of GPAD refresh results and rejection reports."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from standard_annotation_backend.domain.annotation_management import (
    STORED_REJECTION_REPORT,
    AnnotationManagementMode,
    AnnotationRefreshResult,
    AnnotationRefreshUnchanged,
    AnnotationRejection,
    RejectionReport,
)
from standard_annotation_backend.domain.refresh import SourceProvenance
from standard_annotation_backend.domain.stored_json import StoredDataError

_REPORT = RejectionReport(
    issue_count=2,
    by_code={"syntax": 1, "unknown_db_object_id": 1},
    issues=(
        AnnotationRejection(3, "syntax", "bad line"),
        AnnotationRejection(
            9, "unknown_db_object_id", "db_object_id X is not an active entity"
        ),
    ),
)
_LEGACY_REPORT = {
    "issue_count": 2,
    "by_code": {"syntax": 1, "unknown_db_object_id": 1},
    "issues": [
        {"line_number": 3, "code": "syntax", "reason": "bad line"},
        {
            "line_number": 9,
            "code": "unknown_db_object_id",
            "reason": "db_object_id X is not an active entity",
        },
    ],
}


def test_rejection_report_keeps_its_stored_form() -> None:
    """The stored report has the same keys and values as before."""
    assert STORED_REJECTION_REPORT.dump(_REPORT) == _LEGACY_REPORT


def test_stored_rejection_report_loads_back() -> None:
    """A report stored by earlier code loads into an equal report."""
    assert STORED_REJECTION_REPORT.load(_LEGACY_REPORT) == _REPORT


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
        STORED_REJECTION_REPORT.load({**_LEGACY_REPORT, **change})


def test_refresh_job_result_keeps_its_keys() -> None:
    """The published-import job result flattens provenance as before."""
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
        "data_rows": 4,
        "annotations_published": 2,
        "records_rejected": 2,
        "annotations_deleted": 1,
        "mode": "gpad_imported",
        "rejection_report": _LEGACY_REPORT,
    }


def test_unchanged_job_result_keeps_its_keys() -> None:
    """The skipped-refresh job result has the same keys as before."""
    job_id = UUID("00000000-0000-0000-0000-000000000003")
    unchanged = AnnotationRefreshUnchanged("mgi", "MGI", job_id, "c" * 64)
    assert unchanged.to_job_result() == {
        "source_key": "mgi",
        "group_key": "MGI",
        "import_job_id": str(job_id),
        "source_checksum": "c" * 64,
    }
