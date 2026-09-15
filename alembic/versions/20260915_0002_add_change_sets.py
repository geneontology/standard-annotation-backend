"""Add reviewable annotation change sets.

Revision ID: 20260915_0002
Revises: 20260909_0001
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260915_0002"
down_revision: str | None = "20260909_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create proposal storage with operation and review constraints."""
    op.create_table(
        "change_set",
        sa.Column(
            "change_set_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column(
            "state", sa.String(16), server_default=sa.text("'proposed'"), nullable=False
        ),
        sa.Column("owning_group_id", sa.Text(), nullable=False),
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("base_version", sa.Integer(), nullable=True),
        sa.Column(
            "annotation_payload", postgresql.JSONB(none_as_null=True), nullable=True
        ),
        sa.Column("patch", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("preview", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("previewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column(
            "proposed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("result_annotation_version", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("change_set_id", name="pk_change_set"),
        sa.ForeignKeyConstraint(
            ["annotation_id"],
            ["annotation.annotation_id"],
            name="fk_change_set_annotation_id_annotation",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(operation = 'create' AND base_version IS NULL "
            "AND annotation_payload IS NOT NULL "
            "AND jsonb_typeof(annotation_payload) = 'object' AND patch IS NULL "
            "AND ((state = 'accepted' AND annotation_id IS NOT NULL) OR "
            "(state <> 'accepted' AND annotation_id IS NULL))) OR "
            "(operation = 'update' AND annotation_id IS NOT NULL "
            "AND base_version IS NOT NULL AND base_version > 0 "
            "AND annotation_payload IS NULL AND patch IS NOT NULL "
            "AND jsonb_typeof(patch) = 'array') OR "
            "(operation = 'delete' AND annotation_id IS NOT NULL "
            "AND base_version IS NOT NULL AND base_version > 0 "
            "AND annotation_payload IS NULL AND patch IS NULL)",
            name=op.f("ck_change_set_operation_fields_consistent"),
        ),
        sa.CheckConstraint(
            "(state = 'proposed' AND reviewed_by IS NULL "
            "AND reviewed_at IS NULL AND review_reason IS NULL) OR "
            "(state IN ('accepted', 'rejected', 'stale') "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name=op.f("ck_change_set_state_review_consistent"),
        ),
        sa.CheckConstraint(
            "state <> 'rejected' OR "
            "(review_reason IS NOT NULL AND review_reason ~ '[^[:space:]]')",
            name=op.f("ck_change_set_rejection_reason_nonblank"),
        ),
        sa.CheckConstraint(
            "(state = 'accepted' AND result_annotation_version IS NOT NULL "
            "AND result_annotation_version > 0) OR "
            "(state <> 'accepted' AND result_annotation_version IS NULL)",
            name=op.f("ck_change_set_accepted_result_version_consistent"),
        ),
        sa.CheckConstraint(
            "(preview IS NULL AND previewed_at IS NULL) OR "
            "(preview IS NOT NULL AND jsonb_typeof(preview) = 'object' "
            "AND previewed_at IS NOT NULL)",
            name=op.f("ck_change_set_preview_metadata_consistent"),
        ),
    )
    op.create_index("ix_change_set_annotation_id", "change_set", ["annotation_id"])
    op.create_index("ix_change_set_state", "change_set", ["state"])
    op.create_index(
        "ix_change_set_proposed_at_change_set_id",
        "change_set",
        ["proposed_at", "change_set_id"],
    )
    op.create_index("ix_audit_event_change_set_id", "audit_event", ["change_set_id"])


def downgrade() -> None:
    """Remove change-set storage while preserving the existing audit table."""
    op.drop_index("ix_audit_event_change_set_id", table_name="audit_event")
    op.drop_table("change_set")
