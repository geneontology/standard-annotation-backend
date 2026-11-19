"""Define the values and errors used to import a group's annotations from GPAD.

Each annotation source in `config/sources.yaml` names the group that owns every
annotation in its GPAD file. While the group is `gpad_imported`, that file is the
source of truth: each successful refresh deletes all of the group's annotations
and imports the file. A cutover is a final import that succeeds only if every
record is imported. It moves the group to `sabe_managed` permanently, after which
the group's annotations are maintained in SABE and GPAD can no longer replace them.

An import has two steps. Staging reads the file and stores its valid annotations
in separate staging tables without touching the group's annotations. Publication
then replaces the group's annotations with the staged ones in one transaction.

These values need neither HTTP nor a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field

from standard_annotation_backend.domain.errors import ConflictError
from standard_annotation_backend.domain.jobs import JobFailureCode
from standard_annotation_backend.domain.refresh import (
    STORED_REJECTION_REPORT,
    RejectionReport,
    SourceProvenance,
    TerminalRefreshError,
)
from standard_annotation_backend.domain.stored_json import StoredJson


class AnnotationManagementMode(StrEnum):
    """Name the source of truth for a group's annotations.

    `gpad_imported` means the group's GPAD file; `sabe_managed` means SABE. A group
    without a stored mode is `gpad_imported`. The change to `sabe_managed` happens
    only when a cutover publishes, and is permanent.
    """

    GPAD_IMPORTED = "gpad_imported"
    SABE_MANAGED = "sabe_managed"


@dataclass(frozen=True, slots=True)
class AnnotationRefreshResult:
    """Describe a published GPAD import.

    Attributes:
        source_key: Configured annotation source.
        group_key: Group whose annotations were replaced.
        import_job_id: Job that published the import.
        is_cutover: Whether the import was a cutover, which moved the group to
            SABE management.
        provenance: The exact source document that was imported.
        schema_version: Version of the `go-standard-annotation-schema` package the
            file's rows were checked with.
        data_rows: Number of GPAD data rows read, including rejected ones.
        annotations_published: Number of annotations the import published.
        records_rejected: Number of records that were not imported.
        annotations_deleted: Number of the group's earlier annotations that the
            import deleted.
        mode: The group's mode after publication.
        rejection_report: Report of the records that were not imported.
    """

    source_key: str
    group_key: str
    import_job_id: UUID
    is_cutover: bool
    provenance: SourceProvenance
    schema_version: str
    data_rows: Annotated[int, Field(ge=0)]
    annotations_published: Annotated[int, Field(ge=0)]
    records_rejected: Annotated[int, Field(ge=0)]
    annotations_deleted: Annotated[int, Field(ge=0)]
    mode: AnnotationManagementMode
    rejection_report: RejectionReport

    def to_job_result(self) -> dict[str, object]:
        """Return the JSON-compatible result stored on the job.

        The provenance fields are written at the top level of the result.
        """
        stored = STORED_ANNOTATION_REFRESH_RESULT.dump(self)
        provenance = stored.pop("provenance")
        if not isinstance(provenance, dict):
            raise TypeError("provenance must dump to an object")
        return {**stored, **provenance}

    def to_progress(self) -> dict[str, int]:
        """Return the counts reported as job progress."""
        return {
            "data_rows": self.data_rows,
            "annotations_published": self.annotations_published,
            "records_rejected": self.records_rejected,
            "annotations_deleted": self.annotations_deleted,
        }


@dataclass(frozen=True, slots=True)
class AnnotationRefreshUnchanged:
    """Describe a refresh that was skipped because nothing would change.

    The fetched file matches the group's last import, that import was checked with
    the installed schema package version, and the group has not changed since.

    Attributes:
        source_key: Configured annotation source.
        group_key: Group whose annotations were left as they are.
        import_job_id: Job that published the group's current annotations.
        source_checksum: Checksum of the unchanged document.
    """

    source_key: str
    group_key: str
    import_job_id: UUID
    source_checksum: str

    def to_job_result(self) -> dict[str, object]:
        """Return the JSON-compatible result stored on the job."""
        return STORED_ANNOTATION_UNCHANGED_RESULT.dump(self)


STORED_ANNOTATION_REFRESH_RESULT = StoredJson(
    AnnotationRefreshResult, label="annotation refresh result"
)
"""The stored form of the result of a GPAD refresh or cutover that published.

`AnnotationRefreshResult.to_job_result` then moves the provenance fields to the
top level of the job's result, so this form is never loaded back.
"""

STORED_ANNOTATION_UNCHANGED_RESULT = StoredJson(
    AnnotationRefreshUnchanged, label="unchanged annotation refresh result"
)
"""The stored form of the result of a GPAD refresh skipped as unchanged."""


class GroupSabeManagedError(ConflictError, TerminalRefreshError):
    """Report that SABE, not GPAD, is the source of truth for a group.

    GPAD may never replace a `sabe_managed` group's annotations. A refresh job
    fails with `failure_code`, and a request to start one is rejected as a
    conflict. Neither message names the group, so both can be logged safely.

    Attributes:
        group_key: The SABE-managed group.
    """

    failure_code = JobFailureCode.GROUP_SABE_MANAGED
    code = "group_sabe_managed"
    message = (
        "The source's group is managed in SABE, so GPAD can no longer replace its "
        "annotations"
    )

    def __init__(self, group_key: str) -> None:
        self.group_key = group_key
        # Only `TerminalRefreshError` initializes the exception. It comes after
        # the SABE category in the bases, so its `super()` call reaches
        # `Exception` and the text the refresh runner sees is unchanged.
        TerminalRefreshError.__init__(self, "group is SABE-managed")


class AnnotationJobInProgressError(ConflictError):
    """Report that a group already has an unfinished GPAD job of the other type.

    A group has at most one unfinished GPAD job at a time: an ordinary refresh or
    a cutover. Each is reused while unfinished, but a request for one while the
    other is queued or running is rejected, so two jobs never stage or publish
    the same group's annotations at once. The message does not name the group.
    """

    code = "annotation_job_in_progress"
    message = "The source's group already has an unfinished GPAD refresh or cutover job"


class CutoverRejectedError(TerminalRefreshError):
    """Report a cutover that rejected at least one row.

    A cutover publishes only when every row is imported. Staging raises this
    when any row of a cutover's file is rejected. The job's failure details are
    the rejection report.

    Attributes:
        report: The rows that were rejected.
    """

    failure_code = JobFailureCode.CUTOVER_REJECTED

    def __init__(self, report: RejectionReport) -> None:
        self.report = report
        super().__init__(
            "cutover requires zero rejected rows",
            failure_details=STORED_REJECTION_REPORT.dump(report),
        )


class AnnotationImportConflictError(TerminalRefreshError):
    """Report that a group's management row vanished while a publication locked it.

    Publishing a GPAD import creates the group's management row if it is missing
    and then locks it. The row can be missing after that only if another
    transaction deleted it in between, which normal operation never does. The job
    then fails with `invalid_parameters`.
    """

    failure_code = JobFailureCode.INVALID_PARAMETERS

    def __init__(self) -> None:
        super().__init__("job has no usable annotation import")
