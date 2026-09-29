"""add ontology snapshots

Revision ID: 46874af4d8ce
Revises: 113dc6848ea6
Create Date: 2026-09-28 17:29:57.482583
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "46874af4d8ce"
down_revision: str | None = "113dc6848ea6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        "job_type IN ('authorization_sync', 'ontology_load')",
    )
    op.create_table(
        "ontology_metadata",
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column(
            "staging_sequence",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("ontology_key", sa.String(length=100), nullable=False),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("document_version", sa.Text(), nullable=True),
        sa.Column(
            "loaded_predicates", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "load_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.CheckConstraint(
            "source_checksum ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_ontology_metadata_source_checksum_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(loaded_predicates) = 'array' AND NOT jsonb_path_exists(loaded_predicates, '$[*] ? (@.type() != \"string\")')",
            name=op.f("ck_ontology_metadata_loaded_predicates_string_array"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.job_id"],
            name=op.f("fk_ontology_metadata_job_id_job"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("version_id", name=op.f("pk_ontology_metadata")),
        sa.UniqueConstraint("job_id", name=op.f("uq_ontology_metadata_job_id")),
        sa.UniqueConstraint(
            "staging_sequence",
            name=op.f("uq_ontology_metadata_staging_sequence"),
        ),
    )
    op.create_index(
        "ix_ontology_metadata_source",
        "ontology_metadata",
        [
            "ontology_key",
            "source_type",
            "source_locator",
            "source_revision",
            "source_checksum",
        ],
        unique=False,
    )
    op.create_index(
        "uq_ontology_metadata_active_key",
        "ontology_metadata",
        ["ontology_key"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "ontology_term",
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("term_id", sa.Text(), nullable=False),
        sa.Column("obsolete", sa.Boolean(), nullable=False),
        sa.Column(
            "replaced_by", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("consider", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(consider) = 'array' AND NOT jsonb_path_exists(consider, '$[*] ? (@.type() != \"string\")')",
            name=op.f("ck_ontology_term_consider_string_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(replaced_by) = 'array' AND NOT jsonb_path_exists(replaced_by, '$[*] ? (@.type() != \"string\")')",
            name=op.f("ck_ontology_term_replaced_by_string_array"),
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["ontology_metadata.version_id"],
            name=op.f("fk_ontology_term_version_id_ontology_metadata"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("version_id", "term_id", name=op.f("pk_ontology_term")),
    )
    op.create_table(
        "ontology_closure",
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("subject_term_id", sa.Text(), nullable=False),
        sa.Column("predicate_id", sa.Text(), nullable=False),
        sa.Column("object_term_id", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "depth >= 0", name=op.f("ck_ontology_closure_depth_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "object_term_id"],
            ["ontology_term.version_id", "ontology_term.term_id"],
            name="fk_ontology_closure_object",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "subject_term_id"],
            ["ontology_term.version_id", "ontology_term.term_id"],
            name="fk_ontology_closure_subject",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "version_id",
            "subject_term_id",
            "predicate_id",
            "object_term_id",
            name=op.f("pk_ontology_closure"),
        ),
    )
    op.create_index(
        "ix_ontology_closure_object_predicate",
        "ontology_closure",
        ["version_id", "object_term_id", "predicate_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ontology_closure_object_predicate", table_name="ontology_closure")
    op.drop_table("ontology_closure")
    op.drop_table("ontology_term")
    op.drop_index(
        "uq_ontology_metadata_active_key",
        table_name="ontology_metadata",
        postgresql_where=sa.text("active"),
    )
    op.drop_index("ix_ontology_metadata_source", table_name="ontology_metadata")
    op.drop_table("ontology_metadata")
    op.execute(
        "DELETE FROM audit_event WHERE job_id IN "
        "(SELECT job_id FROM job WHERE job_type = 'ontology_load')"
    )
    op.execute("DELETE FROM job WHERE job_type = 'ontology_load'")
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        "job_type IN ('authorization_sync')",
    )
