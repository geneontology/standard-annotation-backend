"""Allow annotation QC report jobs and store their findings and counts.

A QC (quality control) report checks stored annotations and records the issues it
finds in each one. Adds `annotation_qc_report` to the job types the `job` table
accepts, and creates `qc_report_finding`, `qc_report_group_count`, and
`qc_report_issue_count`, whose rows are deleted with their job. Downgrading drops
the tables and deletes QC report jobs and their audit events.

Revision ID: 1becb2618dc3
Revises: 03a2094d9969
Create Date: 2026-11-12 12:06:37.021883
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "1becb2618dc3"
down_revision: str | None = "03a2094d9969"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOB_TYPES = (
    "'annotation_cutover', 'annotation_refresh', 'authorization_refresh', "
    "'entity_refresh', 'entity_retirement', 'ontology_refresh', 'rules_refresh', "
    "'term_replacement'"
)


def _job_id() -> sa.Column:
    return sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False)


def upgrade() -> None:
    """Allow QC report jobs and create their finding and count tables."""
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        f"job_type IN ({_JOB_TYPES}, 'annotation_qc_report')",
    )
    op.create_table(
        "qc_report_finding",
        _job_id(),
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("annotation_version", sa.Integer(), nullable=False),
        sa.Column("owning_group_id", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("issue_types", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(issues) = 'array'",
            name=op.f("ck_qc_report_finding_issues_array"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.job_id"],
            name=op.f("fk_qc_report_finding_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "job_id", "annotation_id", name=op.f("pk_qc_report_finding")
        ),
    )
    op.create_index(
        "ix_qc_report_finding_job_id_owning_group_id",
        "qc_report_finding",
        ["job_id", "owning_group_id"],
    )
    op.create_index(
        "ix_qc_report_finding_issue_types",
        "qc_report_finding",
        ["issue_types"],
        postgresql_using="gin",
    )
    op.create_table(
        "qc_report_group_count",
        _job_id(),
        sa.Column("owning_group_id", sa.Text(), nullable=False),
        sa.Column("annotations_checked", sa.Integer(), nullable=False),
        sa.Column("annotations_with_issues", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.job_id"],
            name=op.f("fk_qc_report_group_count_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "job_id", "owning_group_id", name=op.f("pk_qc_report_group_count")
        ),
    )
    op.create_table(
        "qc_report_issue_count",
        _job_id(),
        sa.Column("owning_group_id", sa.Text(), nullable=False),
        sa.Column("issue_type", sa.Text(), nullable=False),
        sa.Column("annotation_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.job_id"],
            name=op.f("fk_qc_report_issue_count_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "job_id",
            "owning_group_id",
            "issue_type",
            name=op.f("pk_qc_report_issue_count"),
        ),
    )


def downgrade() -> None:
    """Drop the QC report tables and remove QC report jobs."""
    op.drop_table("qc_report_issue_count")
    op.drop_table("qc_report_group_count")
    op.drop_index("ix_qc_report_finding_issue_types", table_name="qc_report_finding")
    op.drop_index(
        "ix_qc_report_finding_job_id_owning_group_id", table_name="qc_report_finding"
    )
    op.drop_table("qc_report_finding")
    op.execute(
        "DELETE FROM audit_event WHERE job_id IN "
        "(SELECT job_id FROM job WHERE job_type = 'annotation_qc_report')"
    )
    op.execute("DELETE FROM job WHERE job_type = 'annotation_qc_report'")
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"), "job", f"job_type IN ({_JOB_TYPES})"
    )
