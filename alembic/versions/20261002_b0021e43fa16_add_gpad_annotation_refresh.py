"""Add GPAD annotation refresh storage.

Adds per-group annotation management modes, per-job GPAD import records, and the
staging tables that publication copies from. Allows the `annotation_refresh` and
`annotation_cutover` job types.

Downgrading deletes every annotation imported by those jobs (with its versions,
comments, change sets, lookup rows, and audit events), then the jobs and their
audit events, because the earlier schema cannot store them. Annotations created
directly in SAB (origin `direct`) are kept.

Revision ID: b0021e43fa16
Revises: 3cd150b4f1f5
Create Date: 2026-10-02 18:08:52.332381
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b0021e43fa16"
down_revision: str | None = "3cd150b4f1f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    op.create_table(
        "annotation_import",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column("is_cutover", sa.Boolean(), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=True),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("data_rows", sa.BigInteger(), nullable=False),
        sa.Column("annotations_staged", sa.BigInteger(), nullable=False),
        sa.Column("records_rejected", sa.BigInteger(), nullable=False),
        sa.Column(
            "rejection_report", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("staged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("annotations_deleted", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "group_key ~ '[^[:space:]]'",
            name=op.f("ck_annotation_import_group_key_nonblank"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rejection_report) = 'object'",
            name=op.f("ck_annotation_import_rejection_report_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_metadata) = 'object'",
            name=op.f("ck_annotation_import_source_metadata_object"),
        ),
        sa.CheckConstraint(
            "source_checksum ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_annotation_import_source_checksum_format"),
        ),
        sa.CheckConstraint(
            "source_key ~ '^[a-z0-9][a-z0-9_-]*$'",
            name=op.f("ck_annotation_import_source_key_format"),
        ),
        sa.CheckConstraint(
            "(published_at IS NULL) = (annotations_deleted IS NULL)",
            name=op.f("ck_annotation_import_publication_complete"),
        ),
        sa.CheckConstraint(
            "data_rows >= 0 AND annotations_staged >= 0 AND records_rejected >= 0",
            name=op.f("ck_annotation_import_counts_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.job_id"],
            name=op.f("fk_annotation_import_job_id_job"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_annotation_import")),
    )
    op.create_index(
        "ix_annotation_import_group_key",
        "annotation_import",
        ["group_key"],
        unique=False,
    )
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
    op.create_table(
        "group_annotation_management",
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column(
            "mode",
            sa.String(length=32),
            server_default=sa.text("'gpad_imported'"),
            nullable=False,
        ),
        sa.Column("last_import_job_id", sa.UUID(), nullable=True),
        sa.Column("transitioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transition_job_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "(mode = 'sab_managed') = (transitioned_at IS NOT NULL) AND (transitioned_at IS NULL) = (transition_job_id IS NULL)",
            name=op.f("ck_group_annotation_management_transition_complete"),
        ),
        sa.CheckConstraint(
            "mode IN ('gpad_imported', 'sab_managed')",
            name=op.f("ck_group_annotation_management_mode_allowed"),
        ),
        sa.CheckConstraint(
            "transition_job_id IS NULL OR transition_job_id = last_import_job_id",
            name=op.f("ck_group_annotation_management_transition_is_last_import"),
        ),
        sa.ForeignKeyConstraint(
            ["last_import_job_id"],
            ["annotation_import.job_id"],
            name=op.f(
                "fk_group_annotation_management_last_import_job_id_annotation_import"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transition_job_id"],
            ["annotation_import.job_id"],
            name=op.f(
                "fk_group_annotation_management_transition_job_id_annotation_import"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "group_key", name=op.f("pk_group_annotation_management")
        ),
    )
    op.create_index(
        "ix_change_set_owning_group_id", "change_set", ["owning_group_id"], unique=False
    )
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        "job_type IN ('annotation_cutover', 'annotation_refresh', "
        "'authorization_refresh', 'entity_refresh', 'entity_retirement', "
        "'ontology_refresh')",
    )


def downgrade() -> None:
    """Revert this revision."""
    annotation_jobs = (
        "SELECT job_id FROM job WHERE job_type IN "
        "('annotation_refresh', 'annotation_cutover')"
    )
    imported = (
        "SELECT annotation_id FROM annotation "
        f"WHERE source_import_job_id IN ({annotation_jobs})"
    )
    op.execute(f"DELETE FROM audit_event WHERE annotation_id IN ({imported})")
    op.execute(
        f"DELETE FROM annotation WHERE source_import_job_id IN ({annotation_jobs})"
    )
    op.drop_index("ix_change_set_owning_group_id", table_name="change_set")
    op.drop_table("group_annotation_management")
    op.drop_table("annotation_staging_multivalued_value")
    op.drop_table("annotation_staging_duplicate_reference")
    op.drop_index(
        "ix_annotation_staging_job_id_db_object_id", table_name="annotation_staging"
    )
    op.drop_table("annotation_staging")
    op.drop_index("ix_annotation_import_group_key", table_name="annotation_import")
    op.drop_table("annotation_import")
    op.execute(f"DELETE FROM audit_event WHERE job_id IN ({annotation_jobs})")
    op.execute(
        "DELETE FROM job WHERE job_type IN ('annotation_refresh', 'annotation_cutover')"
    )
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        "job_type IN ('authorization_refresh', 'entity_refresh', "
        "'entity_retirement', 'ontology_refresh')",
    )
