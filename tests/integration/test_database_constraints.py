"""Tests that PostgreSQL rejects data which violates application rules."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import (
    Engine,
    insert,
    text,
)
from sqlalchemy.exc import IntegrityError

from standard_annotation_backend.domain.annotations import (
    ChangeSource,
)
from standard_annotation_backend.persistence import models
from standard_annotation_backend.persistence.models import (
    AnnotationCommentRecord,
    AnnotationRecord,
    AnnotationVersionRecord,
    JobRecord,
)


def _job_values(
    *,
    job_type: str = "authorization_refresh",
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


def _ordered_job_values() -> dict[str, object]:
    """Build a succeeded job row whose timestamps are in lifecycle order."""
    return _job_values(status="succeeded") | {
        "created_at": datetime(2026, 1, 2, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 3, tzinfo=UTC),
    }


def test_job_with_ordered_lifecycle_timestamps_is_accepted(
    database_engine: Engine,
) -> None:
    """A job whose timestamps follow its lifecycle order can be stored."""
    with database_engine.begin() as connection:
        connection.execute(insert(JobRecord), _ordered_job_values())


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
    values = _ordered_job_values() | changes
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(JobRecord), values)


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


@pytest.mark.parametrize("login", ["curator", "CURATOR"])
def test_github_login_is_unique_ignoring_case(
    database_engine: Engine, login: str
) -> None:
    """GitHub's case-insensitive identity cannot represent two user owners."""
    _auth_owner(database_engine)
    with pytest.raises(IntegrityError), database_engine.begin() as connection:
        connection.execute(insert(models.SabUserRecord), {"github_login": login})


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


def test_authorization_history_restricts_deletion(
    database_engine: Engine,
) -> None:
    """Referenced users, groups, and assignments cannot be deleted while tokens use them."""
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
                "change_source": ChangeSource.API,
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
