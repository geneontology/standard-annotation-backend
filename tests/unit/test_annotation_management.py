"""Verify the contents of GPAD rejection reports."""

from standard_annotation_backend.domain.annotation_management import (
    MAX_REJECTION_REASON_LENGTH,
    MAX_REPORTED_REJECTIONS,
    AnnotationRejection,
    RejectionReportBuilder,
    unknown_subject_rejection,
)


def test_report_counts_every_rejection_but_keeps_the_lowest_100_lines() -> None:
    """The report counts every rejection but lists only the 100 lowest lines.

    The lowest lines are listed even when rejections arrive in reverse order.
    """
    builder = RejectionReportBuilder()
    for line_number in range(250, 0, -1):
        code = "validation" if line_number % 2 else "unknown_db_object_id"
        builder.add(AnnotationRejection(line_number, code, "bad"))

    report = builder.build()

    assert report.issue_count == 250
    assert dict(report.by_code) == {"validation": 125, "unknown_db_object_id": 125}
    assert len(report.issues) == MAX_REPORTED_REJECTIONS
    assert [issue.line_number for issue in report.issues] == list(range(1, 101))


def test_unknown_subject_rejection_names_the_subject_within_the_length_limit() -> None:
    """The rejection names the subject, and a long subject is cut to 200 characters."""
    short = unknown_subject_rejection(7, "UniProtKB:P12345")
    long = unknown_subject_rejection(8, "X:" + "9" * 300)

    assert (short.line_number, short.code) == (7, "unknown_db_object_id")
    assert "UniProtKB:P12345" in short.reason
    assert len(long.reason) == MAX_REJECTION_REASON_LENGTH
