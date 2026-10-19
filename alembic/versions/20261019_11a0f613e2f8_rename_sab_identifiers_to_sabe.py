"""Rename SAB database identifiers to SABE.

The project initialism changed from SAB to SABE. The user and group tables, the
constraints and index named after them, and the stored `sab_managed` annotation
management mode are renamed to match. Downgrading restores the previous names.

Revision ID: 11a0f613e2f8
Revises: e3e60c292b7b
Create Date: 2026-10-19 09:34:18.576397
"""

from collections.abc import Sequence

from alembic import op

revision: str = "11a0f613e2f8"
down_revision: str | None = "e3e60c292b7b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_RENAMES = (("sab_user", "sabe_user"), ("sab_group", "sabe_group"))

# Each entry is (table, old constraint name, new constraint name), where the
# table is named as it exists after the table renames.
_CONSTRAINT_RENAMES = (
    ("sabe_user", "pk_sab_user", "pk_sabe_user"),
    ("sabe_group", "pk_sab_group", "pk_sabe_group"),
    ("sabe_group", "uq_sab_group_group_key", "uq_sabe_group_group_key"),
    (
        "authorization_assignment",
        "fk_authorization_assignment_user_id_sab_user",
        "fk_authorization_assignment_user_id_sabe_user",
    ),
    (
        "authorization_assignment",
        "fk_authorization_assignment_group_id_sab_group",
        "fk_authorization_assignment_group_id_sabe_group",
    ),
    ("api_token", "fk_api_token_user_id_sab_user", "fk_api_token_user_id_sabe_user"),
    (
        "token_management_session",
        "fk_token_management_session_user_id_sab_user",
        "fk_token_management_session_user_id_sabe_user",
    ),
)

_INDEX_RENAMES = (("uq_sab_user_github_login", "uq_sabe_user_github_login"),)

_MODE_CONSTRAINTS = (
    (
        "ck_group_annotation_management_transition_complete",
        "(mode = '{managed}') = (transitioned_at IS NOT NULL) "
        "AND (transitioned_at IS NULL) = (transition_job_id IS NULL)",
    ),
    (
        "ck_group_annotation_management_mode_allowed",
        "mode IN ('gpad_imported', '{managed}')",
    ),
)


def _rename_managed_mode(old: str, new: str) -> None:
    """Move stored annotation management rows and checks to a new mode value."""
    for name, _condition in _MODE_CONSTRAINTS:
        op.drop_constraint(op.f(name), "group_annotation_management", type_="check")
    op.execute(
        f"UPDATE group_annotation_management SET mode = '{new}' WHERE mode = '{old}'"
    )
    for name, condition in _MODE_CONSTRAINTS:
        op.create_check_constraint(
            op.f(name), "group_annotation_management", condition.format(managed=new)
        )


def upgrade() -> None:
    """Apply this revision."""
    for old, new in _TABLE_RENAMES:
        op.rename_table(old, new)
    for table, old, new in _CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new}")
    for old, new in _INDEX_RENAMES:
        op.execute(f"ALTER INDEX {old} RENAME TO {new}")
    _rename_managed_mode("sab_managed", "sabe_managed")


def downgrade() -> None:
    """Revert this revision."""
    _rename_managed_mode("sabe_managed", "sab_managed")
    for old, new in _INDEX_RENAMES:
        op.execute(f"ALTER INDEX {new} RENAME TO {old}")
    for table, old, new in _CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old}")
    for new_table, old_table in ((new, old) for old, new in _TABLE_RENAMES):
        op.rename_table(new_table, old_table)
