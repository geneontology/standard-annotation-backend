"""Tests that PostgreSQL rejects data which violates application rules."""

from datetime import UTC, date, datetime, timedelta
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


def _job_values(
    *,
    job_type: str = "authorization_sync",
    status: str = "queued",
) -> dict[str, object]:
    """Build one valid job row for the requested lifecycle state."""
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "job_type": job_type,
        "status": status,
        "requested_by": "scheduler",
        "parameters": {},
        "progress": {},
        "warnings": [],
        "created_at": now,
        "updated_at": now,
    }
    if status == "running":
        values["started_at"] = now
    elif status == "succeeded":
        values.update(started_at=now, completed_at=now, result={})
    elif status == "failed":
        values.update(completed_at=now, error="Job dispatch failed")
    return values


@pytest.mark.parametrize(
    "job_type",
    [
        "annotation_import",
        "annotation_export",
        "ontology_load",
        "annotation_qc",
        "unknown",
    ],
)
def test_job_constraint_rejects_unimplemented_types(
    database_engine: Engine,
    job_type: str,
) -> None:
    """The database rejects job types without an implemented worker."""
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(JobRecord), _job_values(job_type=job_type))


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed"])
def test_authorization_sync_accepts_each_lifecycle_state(
    database_engine: Engine,
    status: str,
) -> None:
    """An authorization sync job accepts every defined job status."""
    with database_engine.begin() as connection:
        connection.execute(insert(JobRecord), _job_values(status=status))


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "cancelled"},
        {"parameters": []},
        {"progress": []},
        {"warnings": {}},
        {"warnings": ["safe", 7]},
        {"status": "queued", "started_at": datetime.now(UTC)},
        {"status": "queued", "completed_at": datetime.now(UTC)},
        {"status": "queued", "result": {}},
        {"status": "queued", "error": "failed"},
        {"status": "running", "started_at": None},
        {
            "status": "running",
            "started_at": datetime.now(UTC),
            "completed_at": datetime.now(UTC),
        },
        {
            "status": "succeeded",
            "started_at": datetime.now(UTC),
            "completed_at": datetime.now(UTC),
            "result": None,
        },
        {
            "status": "succeeded",
            "started_at": datetime.now(UTC),
            "completed_at": datetime.now(UTC),
            "result": [],
        },
        {
            "status": "succeeded",
            "started_at": datetime.now(UTC),
            "completed_at": datetime.now(UTC),
            "result": {},
            "error": "failed",
        },
        {"status": "failed", "completed_at": datetime.now(UTC), "error": "  "},
        {"status": "failed", "completed_at": None, "error": "failed"},
    ],
)
def test_job_constraints_reject_invalid_shapes_and_transitions(
    database_engine: Engine,
    changes: dict[str, object],
) -> None:
    """The database rejects unsupported values and impossible lifecycle rows."""
    values = _job_values() | changes
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(JobRecord), values)


@pytest.mark.parametrize(
    "changes",
    [
        {"started_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {"completed_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {
            "started_at": datetime(2026, 1, 3, tzinfo=UTC),
            "completed_at": datetime(2026, 1, 2, tzinfo=UTC),
        },
    ],
)
def test_job_constraints_reject_backwards_lifecycle_timestamps(
    database_engine: Engine,
    changes: dict[str, object],
) -> None:
    """Lifecycle timestamps cannot precede creation or each other."""
    values = _job_values(status="succeeded") | {
        "created_at": datetime(2026, 1, 2, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 3, tzinfo=UTC),
    }
    values.update(changes)
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(JobRecord), values)


def test_authorization_source_revision_is_unique(database_engine: Engine) -> None:
    """A repository and commit identify at most one authorization sync record."""
    values = {
        "source_repository": "geneontology/go-site",
        "source_commit_sha": "a" * 40,
        "summary": {},
    }
    with database_engine.begin() as connection:
        connection.execute(insert(models.AuthorizationSyncRecord), values)
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(models.AuthorizationSyncRecord), values)


def _auth_owner(database_engine: Engine) -> tuple[UUID, UUID, UUID]:
    """Persist a user, group, and valid group-scoped assignment."""
    user_id, group_id, assignment_id = uuid4(), uuid4(), uuid4()
    with database_engine.begin() as connection:
        connection.execute(
            insert(models.SabUserRecord),
            {"user_id": user_id, "github_login": "curator"},
        )
        connection.execute(
            insert(models.SabGroupRecord), {"group_id": group_id, "group_key": "MGI"}
        )
        connection.execute(
            insert(models.AuthorizationAssignmentRecord),
            {
                "assignment_id": assignment_id,
                "user_id": user_id,
                "role": "edit",
                "scope": "group",
                "group_id": group_id,
            },
        )
    return user_id, group_id, assignment_id


@pytest.mark.parametrize(
    "role,scope,needs_group",
    [
        ("owner", "global", False),
        ("read", "unknown", False),
        ("read", "self", False),
        ("edit", "group", False),
        ("admin", "global", True),
    ],
)
def test_authorization_constraints_reject_invalid_context(
    database_engine: Engine,
    role: str,
    scope: str,
    needs_group: bool,
) -> None:
    """The database enforces allowed roles and scope-specific group requirements."""
    user_id, group_id, _ = _auth_owner(database_engine)
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(
            insert(models.AuthorizationAssignmentRecord),
            {
                "user_id": user_id,
                "role": role,
                "scope": scope,
                "group_id": group_id if needs_group else None,
            },
        )


@pytest.mark.parametrize("login", ["curator", "CURATOR"])
def test_github_login_is_unique_ignoring_case(
    database_engine: Engine, login: str
) -> None:
    """GitHub's case-insensitive identity cannot represent two user owners."""
    _auth_owner(database_engine)
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(models.SabUserRecord), {"github_login": login})


@pytest.mark.parametrize(
    "role,scope,group",
    [
        ("owner", "group", "MGI"),
        ("read", "unknown", None),
        ("read", "self", None),
        ("edit", "group", None),
        ("admin", "global", "MGI"),
    ],
)
def test_token_snapshot_constraints_reject_invalid_selection(
    database_engine: Engine,
    role: str,
    scope: str,
    group: str | None,
) -> None:
    """Token selections retain valid roles and the group required by their scope."""
    user_id, _, assignment_id = _auth_owner(database_engine)
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO api_token (user_id, assignment_id, name, digest, expires_at, selected_role, selected_scope, selected_group_id) VALUES (:user_id, :assignment_id, 'Client', :digest, :expiry, :role, :scope, :group)"
            ),
            {
                "user_id": user_id,
                "assignment_id": assignment_id,
                "digest": "a" * 64,
                "expiry": datetime.now(UTC) + timedelta(days=1),
                "role": role,
                "scope": scope,
                "group": group,
            },
        )


@pytest.mark.parametrize("scope", ["group", "global"])
def test_active_authorization_contexts_are_unique(
    database_engine: Engine, scope: str
) -> None:
    """Equivalent active grants cannot have distinct IDs, including groupless grants."""
    user_id, group_id, _ = _auth_owner(database_engine)
    values = {
        "user_id": user_id,
        "role": "read",
        "scope": scope,
        "group_id": group_id if scope == "group" else None,
    }
    with database_engine.begin() as connection:
        connection.execute(insert(models.AuthorizationAssignmentRecord), values)
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(models.AuthorizationAssignmentRecord), values)
    with database_engine.begin() as connection:
        connection.execute(
            insert(models.AuthorizationAssignmentRecord), values | {"is_active": False}
        )


@pytest.mark.parametrize("table", ["ApiTokenRecord", "TokenManagementSessionRecord"])
@pytest.mark.parametrize("bad_value", [None, "short", "g" * 64, "A" * 64])
def test_credential_digests_require_lowercase_sha256(
    database_engine: Engine,
    table: str,
    bad_value: str | None,
) -> None:
    """Credentials persist only fixed-length lowercase SHA-256 digests."""
    user_id, _, assignment_id = _auth_owner(database_engine)
    values = {
        "user_id": user_id,
        "digest": bad_value,
        "expires_at": datetime.now(UTC) + timedelta(days=1),
    }
    if table == "ApiTokenRecord":
        values.update(
            assignment_id=assignment_id,
            name="Client",
            selected_role="edit",
            selected_scope="group",
            selected_group_id="MGI",
        )
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(getattr(models, table)), values)


@pytest.mark.parametrize("table", ["ApiTokenRecord", "TokenManagementSessionRecord"])
def test_credential_digest_is_unique(database_engine: Engine, table: str) -> None:
    """A persisted digest identifies at most one credential of its kind."""
    user_id, _, assignment_id = _auth_owner(database_engine)
    values = {
        "user_id": user_id,
        "digest": "a" * 64,
        "expires_at": datetime.now(UTC) + timedelta(days=1),
    }
    if table == "ApiTokenRecord":
        values.update(
            assignment_id=assignment_id,
            name="Client",
            selected_role="edit",
            selected_scope="group",
            selected_group_id="MGI",
        )
    with database_engine.begin() as connection:
        connection.execute(insert(getattr(models, table)), values)
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(getattr(models, table)), values)


@pytest.mark.parametrize("table", ["ApiTokenRecord", "TokenManagementSessionRecord"])
@pytest.mark.parametrize("expires_at", [None, datetime(2000, 1, 1, tzinfo=UTC)])
def test_credentials_require_expiration_after_creation(
    database_engine: Engine,
    table: str,
    expires_at: datetime | None,
) -> None:
    """Persistent credentials always have an expiration later than creation."""
    user_id, _, assignment_id = _auth_owner(database_engine)
    values = {"user_id": user_id, "digest": "a" * 64, "expires_at": expires_at}
    if table == "ApiTokenRecord":
        values.update(
            assignment_id=assignment_id,
            name="Client",
            selected_role="edit",
            selected_scope="group",
            selected_group_id="MGI",
        )
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(getattr(models, table)), values)


def test_token_assignment_must_belong_to_owner(database_engine: Engine) -> None:
    """Even direct writes cannot bind a person's token to another person's grant."""
    _, _, assignment_id = _auth_owner(database_engine)
    other = uuid4()
    with database_engine.begin() as connection:
        connection.execute(
            insert(models.SabUserRecord),
            {"user_id": other, "github_login": "other"},
        )
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(
            insert(models.ApiTokenRecord),
            {
                "user_id": other,
                "assignment_id": assignment_id,
                "selected_role": "edit",
                "selected_scope": "group",
                "selected_group_id": "MGI",
                "name": "Client",
                "digest": "a" * 64,
                "expires_at": datetime.now(UTC) + timedelta(days=1),
            },
        )


def test_authorization_history_restricts_deletion_and_sessions_cascade(
    database_engine: Engine,
) -> None:
    """Referenced users and assignments remain available while sessions follow their owner."""
    user_id, group_id, assignment_id = _auth_owner(database_engine)
    with database_engine.begin() as connection:
        connection.execute(
            insert(models.ApiTokenRecord),
            {
                "user_id": user_id,
                "assignment_id": assignment_id,
                "selected_role": "edit",
                "selected_scope": "group",
                "selected_group_id": "MGI",
                "name": "Client",
                "digest": "a" * 64,
                "expires_at": datetime.now(UTC) + timedelta(days=1),
            },
        )
    for table, column, identity in [
        ("sab_user", "user_id", user_id),
        ("sab_group", "group_id", group_id),
        ("authorization_assignment", "assignment_id", assignment_id),
    ]:
        with pytest.raises(IntegrityError), database_engine.begin() as connection:
            connection.execute(
                text(f"DELETE FROM {table} WHERE {column} = :identity"),
                {"identity": identity},
            )
    with database_engine.begin() as connection:
        other = uuid4()
        connection.execute(
            insert(models.SabUserRecord),
            {"user_id": other, "github_login": "other"},
        )
        connection.execute(
            insert(models.TokenManagementSessionRecord),
            {
                "user_id": other,
                "digest": "b" * 64,
                "expires_at": datetime.now(UTC) + timedelta(minutes=15),
            },
        )
        connection.execute(
            text("DELETE FROM sab_user WHERE user_id = :identity"), {"identity": other}
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM token_management_session"))
            == 0
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
        created_at = datetime.now(UTC)
        with database_engine.begin() as connection:
            connection.execute(
                insert(JobRecord),
                {
                    "job_id": values["source_import_job_id"],
                    "job_type": "authorization_sync",
                    "status": "queued",
                    "created_at": created_at,
                    "updated_at": created_at,
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
