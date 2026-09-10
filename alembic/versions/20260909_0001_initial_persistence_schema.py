"""Initial persistence schema.

Revision ID: 20260909_0001
Revises:
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260909_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the first set of application tables and indexes."""
    op.create_table(
        "job",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column(
            "parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("artifact_uri", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("job_id", name="pk_job"),
    )

    op.create_table(
        "annotation",
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "annotation_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owning_group_id", sa.Text(), nullable=False),
        sa.Column("record_origin", sa.String(length=32), nullable=False),
        sa.Column("source_import_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("duplicate_base_signature", sa.String(length=64), nullable=False),
        sa.Column("db_object_id", sa.Text(), nullable=False),
        sa.Column("negation", sa.Boolean(), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        sa.Column("ontology_class_id", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.Text(), nullable=False),
        sa.Column("annotation_date", sa.Date(), nullable=False),
        sa.Column("assigned_by", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "current_version > 0",
            name=op.f("ck_annotation_current_version_positive"),
        ),
        sa.CheckConstraint(
            "(status = 'active' AND deleted_at IS NULL) OR "
            "(status = 'deleted' AND deleted_at IS NOT NULL)",
            name=op.f("ck_annotation_status_deleted_at_consistent"),
        ),
        sa.CheckConstraint(
            "duplicate_base_signature ~ '^[0-9A-Fa-f]{64}$'",
            name=op.f("ck_annotation_duplicate_base_signature_format"),
        ),
        sa.CheckConstraint(
            "(record_origin = 'import' AND source_import_job_id IS NOT NULL) OR "
            "(record_origin != 'import' AND source_import_job_id IS NULL)",
            name=op.f("ck_annotation_record_origin_source_import_job_id_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["source_import_job_id"],
            ["job.job_id"],
            name="fk_annotation_source_import_job_id_job",
        ),
        sa.PrimaryKeyConstraint("annotation_id", name="pk_annotation"),
    )
    op.create_index("ix_annotation_owning_group_id", "annotation", ["owning_group_id"])
    op.create_index("ix_annotation_record_origin", "annotation", ["record_origin"])
    op.create_index(
        "ix_annotation_source_import_job_id",
        "annotation",
        ["source_import_job_id"],
    )
    op.create_index("ix_annotation_db_object_id", "annotation", ["db_object_id"])
    op.create_index("ix_annotation_negation", "annotation", ["negation"])
    op.create_index("ix_annotation_relation", "annotation", ["relation"])
    op.create_index(
        "ix_annotation_ontology_class_id", "annotation", ["ontology_class_id"]
    )
    op.create_index("ix_annotation_evidence_type", "annotation", ["evidence_type"])
    op.create_index("ix_annotation_annotation_date", "annotation", ["annotation_date"])
    op.create_index("ix_annotation_assigned_by", "annotation", ["assigned_by"])

    op.create_table(
        "annotation_version",
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "annotation_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("change_source", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_annotation_version_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["annotation_id"],
            ["annotation.annotation_id"],
            name="fk_annotation_version_annotation_id_annotation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "annotation_id", "version", name="pk_annotation_version"
        ),
    )

    op.create_table(
        "annotation_multivalued_field_value",
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_name", sa.String(length=32), nullable=False),
        sa.Column("field_value", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "field_name IN ('references', 'with_or_from', 'interacting_taxon_id')",
            name=op.f("ck_annotation_multivalued_field_value_field_name_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["annotation_id"],
            ["annotation.annotation_id"],
            name="fk_annotation_multivalued_field_value_annotation_id_annotation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "annotation_id",
            "field_name",
            "field_value",
            name="pk_annotation_multivalued_field_value",
        ),
    )
    op.create_index(
        "ix_annotation_multivalued_field_value_lookup",
        "annotation_multivalued_field_value",
        ["field_name", "field_value", "annotation_id"],
    )

    op.create_table(
        "annotation_duplicate_reference",
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canonical_reference", sa.Text(), nullable=False),
        sa.Column("duplicate_base_signature", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "duplicate_base_signature ~ '^[0-9A-Fa-f]{64}$'",
            name=op.f(
                "ck_annotation_duplicate_reference_duplicate_base_signature_format"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["annotation_id"],
            ["annotation.annotation_id"],
            name="fk_annotation_duplicate_reference_annotation_id_annotation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "annotation_id",
            "canonical_reference",
            name="pk_annotation_duplicate_reference",
        ),
    )
    op.create_index(
        "ix_annotation_duplicate_reference_conflict_lookup",
        "annotation_duplicate_reference",
        ["duplicate_base_signature", "canonical_reference", "annotation_id"],
        unique=False,
    )

    op.create_table(
        "annotation_comment",
        sa.Column("comment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("annotation_version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "btrim(body) <> ''",
            name=op.f("ck_annotation_comment_body_nonblank"),
        ),
        sa.ForeignKeyConstraint(
            ["annotation_id", "annotation_version"],
            ["annotation_version.annotation_id", "annotation_version.version"],
            name="fk_annotation_comment_annotation_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("comment_id", name="pk_annotation_comment"),
    )

    op.create_table(
        "audit_event",
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("result", sa.String(length=50), nullable=False),
        sa.Column("token_id", sa.Text(), nullable=True),
        sa.Column("token_name", sa.Text(), nullable=True),
        sa.Column("selected_role", sa.String(length=100), nullable=True),
        sa.Column("selected_scope", sa.String(length=100), nullable=True),
        sa.Column("selected_group_id", sa.Text(), nullable=True),
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("annotation_version", sa.Integer(), nullable=True),
        sa.Column("comment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("change_set_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("audit_event_id", name="pk_audit_event"),
    )
    op.create_index("ix_audit_event_annotation_id", "audit_event", ["annotation_id"])
    op.create_index("ix_audit_event_job_id", "audit_event", ["job_id"])


def downgrade() -> None:
    """Remove every table and index created by this revision."""
    op.drop_index("ix_audit_event_job_id", table_name="audit_event")
    op.drop_index("ix_audit_event_annotation_id", table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_table("annotation_comment")
    op.drop_index(
        "ix_annotation_duplicate_reference_conflict_lookup",
        table_name="annotation_duplicate_reference",
    )
    op.drop_table("annotation_duplicate_reference")
    op.drop_index(
        "ix_annotation_multivalued_field_value_lookup",
        table_name="annotation_multivalued_field_value",
    )
    op.drop_table("annotation_multivalued_field_value")
    op.drop_table("annotation_version")
    op.drop_index("ix_annotation_assigned_by", table_name="annotation")
    op.drop_index("ix_annotation_annotation_date", table_name="annotation")
    op.drop_index("ix_annotation_evidence_type", table_name="annotation")
    op.drop_index("ix_annotation_ontology_class_id", table_name="annotation")
    op.drop_index("ix_annotation_relation", table_name="annotation")
    op.drop_index("ix_annotation_negation", table_name="annotation")
    op.drop_index("ix_annotation_db_object_id", table_name="annotation")
    op.drop_index("ix_annotation_source_import_job_id", table_name="annotation")
    op.drop_index("ix_annotation_record_origin", table_name="annotation")
    op.drop_index("ix_annotation_owning_group_id", table_name="annotation")
    op.drop_table("annotation")
    op.drop_table("job")
