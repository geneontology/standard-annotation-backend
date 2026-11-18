"""Verify the rejection reports that GPI and GPAD refreshes record."""

import pytest

from standard_annotation_backend.domain.refresh import (
    MAX_REPORTED_ISSUES,
    STORED_REJECTION_REPORT,
    RejectionReportBuilder,
    RowRejection,
)
from standard_annotation_backend.domain.stored_json import StoredDataError

_REPORT_JSON = {
    "issue_count": 1,
    "by_code": {"syntax": 1},
    "issues": [{"line_number": 3, "code": "syntax", "reason": "bad line"}],
}


def test_report_counts_every_rejection_but_keeps_the_lowest_100_lines() -> None:
    """The report counts every rejection but lists only the 100 lowest lines.

    The lowest lines are listed even when rejections arrive in reverse order.
    """
    builder = RejectionReportBuilder()
    for line_number in range(250, 0, -1):
        code = "validation" if line_number % 2 else "syntax"
        builder.add(RowRejection(line_number, code, "bad"))

    report = builder.build()

    assert report.issue_count == 250
    assert dict(report.by_code) == {"validation": 125, "syntax": 125}
    assert len(report.issues) == MAX_REPORTED_ISSUES
    assert [issue.line_number for issue in report.issues] == list(range(1, 101))


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
