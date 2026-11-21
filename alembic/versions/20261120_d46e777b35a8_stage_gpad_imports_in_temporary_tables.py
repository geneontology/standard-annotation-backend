"""Stage GPAD imports in temporary tables.

Drops `annotation_staging`, `annotation_staging_multivalued_value`, and
`annotation_staging_duplicate_reference`. GPAD refreshes and cutovers now stage in
temporary tables that each import creates in its job's transaction (#76), so these
permanent tables are no longer used. Staging and publication already ran in one
transaction, so any rows left in them were leftovers.

Downgrading recreates the three tables, empty, as they were.

Revision ID: d46e777b35a8
Revises: 407ba0d6660d
Create Date: 2026-11-20 15:36:27.584878
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d46e777b35a8"
down_revision: str | None = "407ba0d6660d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the permanent staging tables, child tables first."""
    op.drop_table("annotation_staging_duplicate_reference")
    op.drop_table("annotation_staging_multivalued_value")
    op.drop_table("annotation_staging")


def downgrade() -> None:
    """Recreate the permanent staging tables as they were before this revision."""
    op.create_table(
        "annotation_staging",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("annotation_id", sa.UUID(), nullable=False),
        sa.Column("line_number", sa.BigInteger(), nullable=False),
        sa.Column(
            "annotation_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("duplicate_base_signature", sa.String(length=64), nullable=False),
        sa.Column("db_object_id", sa.Text(), nullable=False),
        sa.Column("negation", sa.Boolean(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("ontology_class_id", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.Text(), nullable=False),
        sa.Column("annotation_date", sa.Date(), nullable=False),
        sa.Column("assigned_by", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "duplicate_base_signature ~ '^[0-9A-Fa-f]{64}$'",
            name=op.f("ck_annotation_staging_duplicate_base_signature_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(annotation_data) = 'object'",
            name=op.f("ck_annotation_staging_annotation_data_object"),
        ),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_annotation_staging_line_number_positive")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.job_id"],
            name=op.f("fk_annotation_staging_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "job_id", "annotation_id", name=op.f("pk_annotation_staging")
        ),
    )
    op.create_index(
        "ix_annotation_staging_job_id_db_object_id",
        "annotation_staging",
        ["job_id", "db_object_id"],
        unique=False,
    )
    op.create_table(
        "annotation_staging_duplicate_reference",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("annotation_id", sa.UUID(), nullable=False),
        sa.Column("canonical_reference", sa.Text(), nullable=False),
        sa.Column("duplicate_base_signature", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id", "annotation_id"],
            ["annotation_staging.job_id", "annotation_staging.annotation_id"],
            name="fk_annotation_staging_duplicate_reference_staging",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "job_id",
            "annotation_id",
            "canonical_reference",
            name=op.f("pk_annotation_staging_duplicate_reference"),
        ),
    )
    op.create_table(
        "annotation_staging_multivalued_value",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("annotation_id", sa.UUID(), nullable=False),
        sa.Column("field_name", sa.String(length=32), nullable=False),
        sa.Column("field_value", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "field_name IN ('references', 'with_or_from', 'interacting_taxon_id')",
            name=op.f("ck_annotation_staging_multivalued_value_field_name_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "annotation_id"],
            ["annotation_staging.job_id", "annotation_staging.annotation_id"],
            name="fk_annotation_staging_multivalued_value_staging",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "job_id",
            "annotation_id",
            "field_name",
            "field_value",
            name=op.f("pk_annotation_staging_multivalued_value"),
        ),
    )
