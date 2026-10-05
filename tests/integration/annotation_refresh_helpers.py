"""Build GPAD documents and seed GPAD import state for integration tests."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from standard_annotation_backend.domain.annotation_management import (
    AnnotationManagementMode,
)
from standard_annotation_backend.persistence.models import (
    AnnotationImportRecord,
    GroupAnnotationManagementRecord,
    JobRecord,
)

GPAD_HEADER = "!gpad-version: 2.0\n!generated-by: TestDB\n!date-generated: 2026-09-29\n"


def gpad_row(
    db_object_id: str,
    *,
    reference: str = "PMID:1",
    negation: str = "",
    with_or_from: str = "",
    date: str = "2026-09-29",
    assigned_by: str = "MGI",
) -> str:
    """Return one GPAD 2.0 data line without its line ending."""
    return "\t".join(
        [
            db_object_id,
            negation,
            "RO:0002331",
            "GO:0008150",
            reference,
            "ECO:0000314",
            with_or_from,
            "",
            date,
            assigned_by,
            "",
            "",
        ]
    )


def gpad_text(*rows: str) -> str:
    """Return a GPAD 2.0 document with a valid header and the given rows."""
    return GPAD_HEADER + "".join(f"{row}\n" for row in rows)


def gpad_bytes(*rows: str) -> bytes:
    """Return `gpad_text(*rows)` encoded as UTF-8."""
    return gpad_text(*rows).encode()


def seed_group_import(
    session_factory: sessionmaker[Session],
    *,
    group_key: str,
    source_key: str,
    mode: AnnotationManagementMode,
) -> UUID:
    """Record a published import for a group directly, without running a job.

    A `sab_managed` mode records the import as the group's cutover.

    Returns:
        The ID of the succeeded job that the import belongs to.
    """
    now = datetime.now(UTC)
    job_id = uuid4()
    cutover = mode is AnnotationManagementMode.SAB_MANAGED
    with session_factory() as session:
        session.add(
            JobRecord(
                job_id=job_id,
                job_type="annotation_cutover" if cutover else "annotation_refresh",
                status="succeeded",
                requested_by="test",
                parameters={"source_key": source_key},
                progress={},
                warnings=[],
                result={},
                created_at=now,
                updated_at=now,
                started_at=now,
                completed_at=now,
            )
        )
        session.flush()
        session.add(
            AnnotationImportRecord(
                job_id=job_id,
                source_key=source_key,
                group_key=group_key,
                is_cutover=cutover,
                source_type="https",
                source_locator=f"https://example.org/{source_key}.gpad",
                source_revision=None,
                source_checksum="a" * 64,
                fetched_at=now,
                source_metadata={},
                data_rows=0,
                annotations_staged=0,
                records_rejected=0,
                rejection_report={"issue_count": 0, "by_code": {}, "issues": []},
                staged_at=now,
                published_at=now,
                annotations_deleted=0,
            )
        )
        session.flush()
        session.add(
            GroupAnnotationManagementRecord(
                group_key=group_key,
                mode=mode.value,
                last_import_job_id=job_id,
                transitioned_at=now if cutover else None,
                transition_job_id=job_id if cutover else None,
            )
        )
        session.commit()
    return job_id
