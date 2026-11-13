"""Add storage for snapshots of the go-site files that GO Rules read.

A snapshot is the parsed content of the go-site metadata files from one
`rules_refresh` job. Adds `rules_snapshot`, which holds each snapshot and marks at
most one as active, and `retracted_publication`, with one row for each retracted
publication in each snapshot. Allows the `rules_refresh` job type. Downgrading
drops both tables, then deletes `rules_refresh` jobs and their audit events,
because the earlier schema cannot store them.

Revision ID: 1d87c729df70
Revises: 736204438f1e
Create Date: 2026-11-10 13:50:47.819940
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "1d87c729df70"
down_revision: str | None = "736204438f1e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    op.create_table(
        "rules_snapshot",
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=True),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("rule_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("retracted_publication_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "source_checksum ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_rules_snapshot_source_checksum_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rule_data) = 'object'",
            name=op.f("ck_rules_snapshot_rule_data_object"),
        ),
        sa.CheckConstraint(
            "retracted_publication_count >= 0",
            name=op.f("ck_rules_snapshot_retracted_publication_count_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.job_id"],
            name=op.f("fk_rules_snapshot_job_id_job"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_rules_snapshot")),
        sa.UniqueConstraint("job_id", name=op.f("uq_rules_snapshot_job_id")),
    )
    op.create_index(
        "uq_rules_snapshot_active",
        "rules_snapshot",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "retracted_publication",
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("pmid", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "pmid ~ '^PMID:[0-9]+$'", name=op.f("ck_retracted_publication_pmid_format")
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["rules_snapshot.snapshot_id"],
            name=op.f("fk_retracted_publication_snapshot_id_rules_snapshot"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_id", "pmid", name=op.f("pk_retracted_publication")
        ),
    )
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        "job_type IN ('annotation_cutover', 'annotation_refresh', "
        "'authorization_refresh', 'entity_refresh', 'entity_retirement', "
        "'ontology_refresh', 'rules_refresh')",
    )


def downgrade() -> None:
    """Revert this revision."""
    op.drop_table("retracted_publication")
    op.drop_table("rules_snapshot")
    op.execute(
        "DELETE FROM audit_event WHERE job_id IN "
        "(SELECT job_id FROM job WHERE job_type = 'rules_refresh')"
    )
    op.execute("DELETE FROM job WHERE job_type = 'rules_refresh'")
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        "job_type IN ('annotation_cutover', 'annotation_refresh', "
        "'authorization_refresh', 'entity_refresh', 'entity_retirement', "
        "'ontology_refresh')",
    )
