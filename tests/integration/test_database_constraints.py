"""Tests that PostgreSQL rejects data which violates application rules."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import CheckConstraint, Engine, insert, inspect, text
from sqlalchemy.exc import IntegrityError

from standard_annotation_backend.persistence import models
from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AnnotationMultivaluedFieldValueRecord,
    AnnotationOrigin,
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
        {"operation": "unsupported"},
        {"state": "unsupported"},
        {"annotation_payload": None},
        {"annotation_payload": []},
        {"patch": []},
        {"base_version": 1},
        {"annotation_id": uuid4()},
        {"operation": "update", "annotation_payload": None, "patch": []},
        {"operation": "delete", "annotation_payload": None},
        {"reviewed_by": "reviewer"},
        {"reviewed_at": datetime.now(UTC)},
        {"review_reason": "Unfinished review"},
        {"state": "rejected", "review_reason": "Declined"},
        {
            "state": "rejected",
            "reviewed_by": "reviewer",
            "reviewed_at": datetime.now(UTC),
        },
        {
            "state": "rejected",
            "reviewed_by": "reviewer",
            "reviewed_at": datetime.now(UTC),
            "review_reason": " \t\n",
        },
        {
            "state": "accepted",
            "reviewed_by": "reviewer",
            "reviewed_at": datetime.now(UTC),
        },
        {"result_annotation_version": 1},
        {"state": "stale", "reviewed_by": "reviewer"},
        {"preview": []},
        {"preview": {}},
        {"previewed_at": datetime.now(UTC)},
    ],
)
def test_change_set_constraints_reject_invalid_operation_and_review_shapes(
    database_engine: Engine,
    changes: dict[str, object],
) -> None:
    """Direct database writes must obey proposal and review field requirements."""
    values = {
        "operation": "create",
        "state": "proposed",
        "owning_group_id": "group",
        "annotation_payload": {},
        "proposed_by": "proposer",
        "reason": "Evidence",
    } | changes
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(models.ChangeSetRecord), values)


def test_change_set_database_generates_identity_and_proposal_time(
    database_engine: Engine,
) -> None:
    """Raw inserts receive the same server defaults as repository proposals."""
    with database_engine.begin() as connection:
        row = connection.execute(
            text(
                "INSERT INTO change_set (operation, owning_group_id, annotation_payload, proposed_by, reason) "
                "VALUES ('create', 'group', '{}'::jsonb, 'proposer', 'Evidence') "
                "RETURNING change_set_id, proposed_at, state"
            )
        ).one()
        assert isinstance(row.change_set_id, UUID)
        assert row.proposed_at.tzinfo is UTC
        assert row.state == "proposed"


def test_change_set_migration_constraints_match_model_metadata(
    database_engine: Engine,
) -> None:
    """Migration constraint names match those used by later schema operations."""
    deployed = {
        constraint["name"]
        for constraint in inspect(database_engine).get_check_constraints("change_set")
    }
    declared = {
        constraint.name
        for constraint in models.Base.metadata.tables["change_set"].constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert deployed == declared


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize(
    "changes",
    [
        {"annotation_id": None},
        {"base_version": None},
        {"base_version": 0},
        {"annotation_payload": {}},
        {"patch": {}},
        {
            "state": "accepted",
            "reviewed_by": "reviewer",
            "reviewed_at": datetime.now(UTC),
        },
        {
            "state": "accepted",
            "reviewed_by": "reviewer",
            "reviewed_at": datetime.now(UTC),
            "result_annotation_version": 0,
        },
        {
            "state": "rejected",
            "reviewed_by": "reviewer",
            "reviewed_at": datetime.now(UTC),
            "review_reason": "Declined",
            "result_annotation_version": 2,
        },
        {
            "state": "stale",
            "reviewed_by": "reviewer",
            "reviewed_at": datetime.now(UTC),
            "result_annotation_version": 2,
        },
    ],
)
def test_targeted_change_set_constraints(
    database_engine: Engine,
    operation: str,
    changes: dict[str, object],
) -> None:
    """Targeted proposals require a positive base version and accepted result version."""
    annotation_id = uuid4()
    with database_engine.begin() as connection:
        connection.execute(insert(AnnotationRecord), _annotation_values(annotation_id))
    values = {
        "operation": operation,
        "state": "proposed",
        "owning_group_id": "group",
        "annotation_id": annotation_id,
        "base_version": 1,
        "patch": [{"op": "replace", "path": "/assigned_by", "value": "TEST"}]
        if operation == "update"
        else None,
        "proposed_by": "proposer",
        "reason": "Evidence",
    } | changes
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(models.ChangeSetRecord), values)


@pytest.mark.parametrize(
    "changes",
    [
        {"current_version": 0},
        {"status": "deleted", "deleted_at": None},
        {"status": "active", "deleted_at": datetime.now(UTC)},
        {"status": "pending"},
        {"duplicate_base_signature": "0" * 63},
        {"record_origin": AnnotationOrigin.DIRECT, "source_import_job_id": uuid4()},
        {"record_origin": AnnotationOrigin.IMPORT, "source_import_job_id": None},
        {"record_origin": "unsupported", "source_import_job_id": None},
    ],
)
def test_annotation_constraints_reject_invalid_current_rows(
    database_engine: Engine,
    changes: dict[str, object],
) -> None:
    """The database rejects invalid versions, states, signatures, and provenance."""
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


def test_comment_constraint_rejects_whitespace_only_body(
    database_engine: Engine,
) -> None:
    """The database rejects a comment containing only whitespace."""
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
                "annotation_version": 1,
                "body": "\t\n",
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
