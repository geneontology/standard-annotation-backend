"""Tests that PostgreSQL rejects data which violates application rules."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, insert
from sqlalchemy.exc import IntegrityError

from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AnnotationMultivaluedFieldValueRecord,
    AnnotationRecord,
    AnnotationVersionRecord,
    JobRecord,
)


def _annotation_values(annotation_id: UUID) -> dict[str, object]:
    return {
        "annotation_id": annotation_id,
        "annotation_data": {},
        "current_version": 1,
        "status": "active",
        "deleted_at": None,
        "owning_group_id": "test-group",
        "record_origin": "direct",
        "source_import_job_id": None,
        "duplicate_base_signature": "0" * 64,
        "db_object_id": "UniProtKB:P12345",
        "negation": False,
        "relation": "enables",
        "ontology_class_id": "GO:0003674",
        "evidence_type": "ECO:0000314",
        "annotation_date": date(2026, 1, 1),
        "assigned_by": "TEST",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"current_version": 0},
        {"status": "deleted", "deleted_at": None},
        {"status": "active", "deleted_at": datetime.now(UTC)},
        {"status": "pending"},
        {"duplicate_base_signature": "0" * 63},
        {"record_origin": "direct", "source_import_job_id": uuid4()},
        {"record_origin": "import", "source_import_job_id": None},
    ],
)
def test_annotation_constraints_reject_invalid_current_rows(
    database_engine: Engine,
    changes: dict[str, object],
) -> None:
    """The database rejects invalid versions, states, signatures, and provenance”."""
    values = _annotation_values(uuid4()) | changes
    if changes.get("source_import_job_id") is not None:
        with database_engine.begin() as connection:
            connection.execute(
                insert(JobRecord),
                {
                    "job_id": values["source_import_job_id"],
                    "job_type": "import",
                    "status": "completed",
                    "created_at": datetime.now(UTC),
                    "requested_by": "test-user",
                },
            )

    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(AnnotationRecord), values)


def test_annotation_version_constraint_rejects_nonpositive_versions(
    database_engine: Engine,
) -> None:
    """The database rejects annotation history with a nonpositive version."""
    annotation_id = uuid4()
    with database_engine.begin() as connection:
        connection.execute(
            insert(AnnotationRecord),
            _annotation_values(annotation_id),
        )

    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(
            insert(AnnotationVersionRecord),
            {
                "annotation_id": annotation_id,
                "version": 0,
                "annotation_data": {},
                "is_deleted": False,
                "actor_id": "test-user",
                "change_source": "test",
            },
        )


def test_comment_constraint_rejects_unknown_annotation_version(
    database_engine: Engine,
) -> None:
    """The database rejects comments attached to a version that does not exist."""
    annotation_id = uuid4()
    with database_engine.begin() as connection:
        connection.execute(insert(AnnotationRecord), _annotation_values(annotation_id))
        connection.execute(
            insert(AnnotationVersionRecord),
            {
                "annotation_id": annotation_id,
                "version": 1,
                "annotation_data": {},
                "is_deleted": False,
                "actor_id": "test-user",
                "change_source": "test",
            },
        )

    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(
            insert(AnnotationCommentRecord),
            {
                "comment_id": uuid4(),
                "annotation_id": annotation_id,
                "annotation_version": 2,
                "body": "References a missing version",
                "created_by": "test-user",
                "deleted_at": None,
            },
        )


def test_multivalued_field_constraint_rejects_unknown_fields(
    database_engine: Engine,
) -> None:
    """The database rejects fields that are not stored as multivalued values."""
    annotation_id = uuid4()
    with database_engine.begin() as connection:
        connection.execute(
            insert(AnnotationRecord),
            _annotation_values(annotation_id),
        )

    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(
            insert(AnnotationMultivaluedFieldValueRecord),
            {
                "annotation_id": annotation_id,
                "field_name": "assigned_by",
                "field_value": "TEST",
            },
        )
