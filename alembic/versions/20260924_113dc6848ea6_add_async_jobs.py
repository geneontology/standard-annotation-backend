"""Add asynchronous job state and prevent duplicate source revisions.

Revision ID: 113dc6848ea6
Revises: e063f7c1bed0
Create Date: 2026-09-24 10:59:36.439175
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "113dc6848ea6"
down_revision: str | None = "e063f7c1bed0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add job-state constraints and unique authorization-source revisions."""
    # SAB has no retained deployments or historical data at this project stage, so
    # the source-revision constraint can be added directly without reconciling rows.
    op.create_unique_constraint(
        "source_revision_unique",
        "authorization_sync",
        ["source_repository", "source_commit_sha"],
    )
    op.add_column(
        "job",
        sa.Column(
            "progress",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "job",
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "job",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        "job_type IN ('authorization_sync')",
    )
    op.create_check_constraint(
        op.f("ck_job_parameters_object"),
        "job",
        "jsonb_typeof(parameters) = 'object'",
    )
    op.create_check_constraint(
        op.f("ck_job_progress_object"),
        "job",
        "jsonb_typeof(progress) = 'object'",
    )
    op.create_check_constraint(
        op.f("ck_job_warnings_string_array"),
        "job",
        "jsonb_typeof(warnings) = 'array' AND NOT "
        "jsonb_path_exists(warnings, '$[*] ? (@.type() != \"string\")')",
    )
    op.create_check_constraint(
        op.f("ck_job_result_object"),
        "job",
        "result IS NULL OR jsonb_typeof(result) = 'object'",
    )
    op.create_check_constraint(
        op.f("ck_job_lifecycle_consistent"),
        "job",
        "(status = 'queued' AND started_at IS NULL "
        "AND completed_at IS NULL AND result IS NULL AND error IS NULL) OR "
        "(status = 'running' AND started_at IS NOT NULL "
        "AND completed_at IS NULL AND result IS NULL AND error IS NULL) OR "
        "(status = 'succeeded' AND started_at IS NOT NULL "
        "AND completed_at IS NOT NULL AND result IS NOT NULL "
        "AND error IS NULL) OR "
        "(status = 'failed' AND completed_at IS NOT NULL "
        "AND result IS NULL AND error IS NOT NULL "
        "AND error ~ '[^[:space:]]')",
    )
    op.create_check_constraint(
        op.f("ck_job_timestamps_ordered"),
        "job",
        "updated_at >= created_at "
        "AND (started_at IS NULL OR started_at >= created_at) "
        "AND (completed_at IS NULL OR completed_at >= created_at) "
        "AND (started_at IS NULL OR completed_at IS NULL "
        "OR completed_at >= started_at)",
    )
    op.create_index(
        "ix_job_status_created_at_job_id",
        "job",
        ["status", "created_at", "job_id"],
    )


def downgrade() -> None:
    """Remove the constraints, index, and columns added by `upgrade`."""
    op.drop_index("ix_job_status_created_at_job_id", table_name="job")
    op.drop_constraint(op.f("ck_job_timestamps_ordered"), "job", type_="check")
    op.drop_constraint(op.f("ck_job_lifecycle_consistent"), "job", type_="check")
    op.drop_constraint(op.f("ck_job_result_object"), "job", type_="check")
    op.drop_constraint(op.f("ck_job_warnings_string_array"), "job", type_="check")
    op.drop_constraint(op.f("ck_job_progress_object"), "job", type_="check")
    op.drop_constraint(op.f("ck_job_parameters_object"), "job", type_="check")
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.drop_column("job", "updated_at")
    op.drop_column("job", "warnings")
    op.drop_column("job", "progress")
    op.drop_constraint("source_revision_unique", "authorization_sync", type_="unique")
