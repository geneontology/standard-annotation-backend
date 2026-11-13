"""Verify the contents of GPAD rejection reports."""

from standard_annotation_backend.domain.annotation_management import (
    MAX_REPORTED_REJECTIONS,
    AnnotationRejection,
    RejectionReportBuilder,
)


def test_report_counts_every_rejection_but_keeps_the_lowest_100_lines() -> None:
    """The report counts every rejection but lists only the 100 lowest lines.

    The lowest lines are listed even when rejections arrive in reverse order.
    """
    builder = RejectionReportBuilder()
    for line_number in range(250, 0, -1):
        code = "validation" if line_number % 2 else "syntax"
        builder.add(AnnotationRejection(line_number, code, "bad"))

    report = builder.build()

    assert report.issue_count == 250
    assert dict(report.by_code) == {"validation": 125, "syntax": 125}
    assert len(report.issues) == MAX_REPORTED_REJECTIONS
    assert [issue.line_number for issue in report.issues] == list(range(1, 101))
