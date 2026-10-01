"""add entity catalogs

Revision ID: 7278732b121d
Revises: b971c66386e0
Create Date: 2026-09-30 12:46:18.328276
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7278732b121d"
down_revision: str | None = "b971c66386e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the entity catalog tables and allow entity import and retirement jobs."""
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        "job_type IN ('authorization_sync', 'entity_catalog_retirement', 'entity_import', 'ontology_load')",
    )
    op.create_table(
        "entity_catalog_snapshot",
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("source_format", sa.String(length=20), nullable=False),
        sa.Column(
            "source_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "source_statistics", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "record_statistics", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "publication_result",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_job_id", sa.UUID(), nullable=True),
        sa.Column(
            "retirement_result",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("staged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_metadata) = 'object'",
            name=op.f("ck_entity_catalog_snapshot_source_metadata_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(record_statistics) = 'object'",
            name=op.f("ck_entity_catalog_snapshot_record_statistics_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_statistics) = 'object'",
            name=op.f("ck_entity_catalog_snapshot_source_statistics_object"),
        ),
        sa.CheckConstraint(
            "publication_result IS NULL OR jsonb_typeof(publication_result) = 'object'",
            name=op.f("ck_entity_catalog_snapshot_publication_result_object"),
        ),
        sa.CheckConstraint(
            "source_checksum ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_entity_catalog_snapshot_source_checksum_format"),
        ),
        sa.CheckConstraint(
            "source_key ~ '^[a-z0-9][a-z0-9_-]*$'",
            name=op.f("ck_entity_catalog_snapshot_source_key_format"),
        ),
        sa.CheckConstraint(
            "(retired_at IS NULL) = (retired_by_job_id IS NULL) "
            "AND (retired_at IS NULL) = (retirement_result IS NULL)",
            name=op.f("ck_entity_catalog_snapshot_retirement_complete"),
        ),
        sa.CheckConstraint(
            "retired_at IS NULL OR NOT active",
            name=op.f("ck_entity_catalog_snapshot_retired_inactive"),
        ),
        sa.CheckConstraint(
            "retirement_result IS NULL OR jsonb_typeof(retirement_result) = 'object'",
            name=op.f("ck_entity_catalog_snapshot_retirement_result_object"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.job_id"],
            name=op.f("fk_entity_catalog_snapshot_job_id_job"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by_job_id"],
            ["job.job_id"],
            name=op.f("fk_entity_catalog_snapshot_retired_by_job_id_job"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_entity_catalog_snapshot")),
        sa.UniqueConstraint("job_id", name=op.f("uq_entity_catalog_snapshot_job_id")),
        sa.UniqueConstraint(
            "retired_by_job_id",
            name=op.f("uq_entity_catalog_snapshot_retired_by_job_id"),
        ),
    )
    op.create_index(
        "uq_entity_catalog_snapshot_active_source",
        "entity_catalog_snapshot",
        ["source_key"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "entity_staging_record",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("line_number", sa.BigInteger(), nullable=False),
        sa.Column("db_object_id", sa.Text(), nullable=False),
        sa.Column("entity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(entity) = 'object'",
            name=op.f("ck_entity_staging_record_entity_object"),
        ),
        sa.CheckConstraint(
            "line_number > 0",
            name=op.f("ck_entity_staging_record_line_number_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.job_id"],
            name=op.f("fk_entity_staging_record_job_id_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "job_id", "line_number", name=op.f("pk_entity_staging_record")
        ),
    )
    op.create_index(
        "ix_entity_staging_record_db_object_id",
        "entity_staging_record",
        ["db_object_id"],
        unique=False,
    )
    op.create_table(
        "entity_membership",
        sa.Column("db_object_id", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "source_key ~ '^[a-z0-9][a-z0-9_-]*$'",
            name=op.f("ck_entity_membership_source_key_format"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["entity_catalog_snapshot.snapshot_id"],
            name=op.f("fk_entity_membership_snapshot_id_entity_catalog_snapshot"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("db_object_id", name=op.f("pk_entity_membership")),
    )
    op.create_index(
        "ix_entity_membership_source_key",
        "entity_membership",
        ["source_key"],
        unique=False,
    )
    op.create_table(
        "entity_source_record",
        sa.Column("db_object_id", sa.Text(), nullable=False),
        sa.Column("line_number", sa.BigInteger(), nullable=False),
        sa.Column("entity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(entity) = 'object'",
            name=op.f("ck_entity_source_record_entity_object"),
        ),
        sa.CheckConstraint(
            "line_number > 0",
            name=op.f("ck_entity_source_record_line_number_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["db_object_id"],
            ["entity_membership.db_object_id"],
            name=op.f("fk_entity_source_record_db_object_id_entity_membership"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "db_object_id", "line_number", name=op.f("pk_entity_source_record")
        ),
    )


def downgrade() -> None:
    """Remove the entity catalog tables and entity jobs.

    Entity jobs and their audit events are deleted so the previous job type check
    constraint can be restored.
    """
    op.drop_table("entity_source_record")
    op.drop_index(
        "ix_entity_membership_source_key",
        table_name="entity_membership",
    )
    op.drop_table("entity_membership")
    op.drop_index(
        "ix_entity_staging_record_db_object_id", table_name="entity_staging_record"
    )
    op.drop_table("entity_staging_record")
    op.drop_index(
        "uq_entity_catalog_snapshot_active_source",
        table_name="entity_catalog_snapshot",
        postgresql_where=sa.text("active"),
    )
    op.drop_table("entity_catalog_snapshot")
    op.execute(
        "DELETE FROM audit_event WHERE job_id IN (SELECT job_id FROM job WHERE "
        "job_type IN ('entity_import', 'entity_catalog_retirement'))"
    )
    op.execute(
        "DELETE FROM job WHERE job_type IN ('entity_import', 'entity_catalog_retirement')"
    )
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        "job_type IN ('authorization_sync', 'ontology_load')",
    )
