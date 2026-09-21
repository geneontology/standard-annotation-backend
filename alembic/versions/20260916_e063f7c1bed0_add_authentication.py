"""Add synchronized user identities, grants, and credential digests.

Revision ID: e063f7c1bed0
Revises: a43f54366f2c
Create Date: 2026-09-16 13:53:38.604680
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e063f7c1bed0"
down_revision: str | None = "a43f54366f2c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create normalized user authorization and credential storage."""
    op.create_table(
        "sab_user",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("github_login", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_sab_user"),
    )
    op.create_index(
        "uq_sab_user_github_login",
        "sab_user",
        [sa.text("lower(github_login)")],
        unique=True,
    )
    op.create_table(
        "sab_group",
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("group_id", name="pk_sab_group"),
        sa.UniqueConstraint("group_key", name="uq_sab_group_group_key"),
    )
    op.create_table(
        "authorization_assignment",
        sa.Column(
            "assignment_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("assignment_id", name="pk_authorization_assignment"),
        sa.UniqueConstraint(
            "assignment_id", "user_id", name="uq_authorization_assignment_assignment_id"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["sab_user.user_id"],
            ondelete="RESTRICT",
            name="fk_authorization_assignment_user_id_sab_user",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["sab_group.group_id"],
            ondelete="RESTRICT",
            name="fk_authorization_assignment_group_id_sab_group",
        ),
        sa.CheckConstraint(
            "role IN ('read', 'edit', 'admin')",
            name=op.f("ck_authorization_assignment_role_allowed"),
        ),
        sa.CheckConstraint(
            "(scope IN ('self', 'group') AND group_id IS NOT NULL) OR (scope = 'global' AND group_id IS NULL)",
            name=op.f("ck_authorization_assignment_scope_group_consistent"),
        ),
    )
    op.create_index(
        "uq_authorization_assignment_active_context",
        "authorization_assignment",
        ["user_id", "role", "scope", "group_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "ix_authorization_assignment_group_id", "authorization_assignment", ["group_id"]
    )
    op.create_table(
        "api_token",
        sa.Column(
            "token_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selected_role", sa.String(16), nullable=False),
        sa.Column("selected_scope", sa.String(16), nullable=False),
        sa.Column("selected_group_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token_id", name="pk_api_token"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["sab_user.user_id"],
            ondelete="RESTRICT",
            name="fk_api_token_user_id_sab_user",
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id", "user_id"],
            [
                "authorization_assignment.assignment_id",
                "authorization_assignment.user_id",
            ],
            ondelete="RESTRICT",
            name="fk_api_token_assignment_owner",
        ),
        sa.CheckConstraint(
            "digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_api_token_digest_format")
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_api_token_expiration_after_creation"),
        ),
        sa.CheckConstraint(
            "selected_role IN ('read', 'edit', 'admin')",
            name=op.f("ck_api_token_selected_role_allowed"),
        ),
        sa.CheckConstraint(
            "(selected_scope IN ('self', 'group') AND selected_group_id IS NOT NULL) OR (selected_scope = 'global' AND selected_group_id IS NULL)",
            name=op.f("ck_api_token_selected_scope_group_consistent"),
        ),
    )
    op.create_index("ix_api_token_digest", "api_token", ["digest"], unique=True)
    op.create_index("ix_api_token_user_id", "api_token", ["user_id"])
    op.create_index("ix_api_token_assignment_id", "api_token", ["assignment_id"])
    op.create_table(
        "token_management_session",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("session_id", name="pk_token_management_session"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["sab_user.user_id"],
            ondelete="CASCADE",
            name="fk_token_management_session_user_id_sab_user",
        ),
        sa.CheckConstraint(
            "digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_token_management_session_digest_format"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_token_management_session_expiration_after_creation"),
        ),
    )
    op.create_index(
        "ix_token_management_session_digest",
        "token_management_session",
        ["digest"],
        unique=True,
    )
    op.create_index(
        "ix_token_management_session_user_id", "token_management_session", ["user_id"]
    )
    op.create_table(
        "authorization_sync",
        sa.Column(
            "sync_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("source_repository", sa.Text(), nullable=False),
        sa.Column("source_commit_sha", sa.Text(), nullable=False),
        sa.Column(
            "synchronized_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("sync_id", name="pk_authorization_sync"),
    )


def downgrade() -> None:
    """Remove authentication tables in foreign-key dependency order."""
    op.drop_table("authorization_sync")
    op.drop_table("token_management_session")
    op.drop_table("api_token")
    op.drop_table("authorization_assignment")
    op.drop_table("sab_group")
    op.drop_table("sab_user")
