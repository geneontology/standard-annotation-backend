"""Align reference data refresh storage.

Renames authorization synchronization, ontology loading, and entity import storage
to the shared "refresh" vocabulary. It aligns snapshot provenance on `source_type`,
`source_locator`, `source_revision`, `source_checksum`, and `fetched_at`.

SAB has no deployments with important data at this stage (see revision
`113dc6848ea6`). Instead of converting stored jobs, results, and audit details
between vocabularies, both directions delete reference data and job history. Run
`just refresh` for each kind after migrating. Users, grants, tokens, annotations,
versions, comments, and change sets are kept. Audit events of annotation updates
made by ontology refresh jobs are deleted too, because they carry a job ID; the
annotation versions those jobs created are kept.

Revision ID: 3cd150b4f1f5
Revises: 7278732b121d
Create Date: 2026-10-01 13:20:26.640013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3cd150b4f1f5"
down_revision: str | None = "7278732b121d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CLEAR_REFERENCE_DATA = (
    "DELETE FROM entity_source_record",
    "DELETE FROM entity_membership",
    "DELETE FROM entity_staging_record",
    "DELETE FROM entity_catalog_snapshot",
    "DELETE FROM ontology_closure",
    "DELETE FROM ontology_term",
    "DELETE FROM ontology_metadata",
    "DELETE FROM audit_event WHERE job_id IS NOT NULL OR action IN ("
    "'authorization.synchronized', 'ontology.loaded', 'entity.catalog_published', "
    "'entity.catalog_retired', 'authorization.refreshed', 'ontology.refreshed', "
    "'entity.refreshed', 'entity.retired')",
    # `annotation.source_import_job_id` references `job`. No job type can create
    # imported annotations yet, so no job is referenced. Do not use TRUNCATE ...
    # CASCADE here, which would also empty `annotation`.
    "DELETE FROM job",
)


def _clear_reference_data(authorization_table: str) -> None:
    """Delete reference data and job history in foreign-key order."""
    for statement in _CLEAR_REFERENCE_DATA:
        op.execute(statement)
    op.execute(f"DELETE FROM {authorization_table}")


def upgrade() -> None:
    """Clear reference data and job history, then rename refresh storage."""
    _clear_reference_data("authorization_sync")
    op.execute(
        "UPDATE annotation_version SET change_source = 'ontology_refresh' "
        "WHERE change_source = 'ontology_load'"
    )

    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        "job_type IN ('authorization_refresh', 'entity_refresh', "
        "'entity_retirement', 'ontology_refresh')",
    )

    op.drop_table("authorization_sync")
    op.create_table(
        "authorization_refresh",
        sa.Column(
            "refresh_id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
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
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "source_checksum ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_authorization_refresh_source_checksum_format"),
        ),
        sa.PrimaryKeyConstraint("refresh_id", name=op.f("pk_authorization_refresh")),
    )

    op.alter_column("ontology_metadata", "loaded_at", new_column_name="fetched_at")
    op.alter_column(
        "ontology_metadata", "load_result", new_column_name="refresh_result"
    )
    op.alter_column(
        "ontology_metadata", "source_revision", existing_type=sa.Text(), nullable=True
    )

    op.alter_column(
        "entity_catalog_snapshot", "source_url", new_column_name="source_locator"
    )
    op.add_column(
        "entity_catalog_snapshot",
        sa.Column("source_type", sa.String(length=20), nullable=False),
    )
    op.add_column(
        "entity_catalog_snapshot",
        sa.Column("source_revision", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Clear reference data and job history, then restore the previous storage."""
    _clear_reference_data("authorization_refresh")
    op.execute(
        "UPDATE annotation_version SET change_source = 'ontology_load' "
        "WHERE change_source = 'ontology_refresh'"
    )

    op.drop_column("entity_catalog_snapshot", "source_revision")
    op.drop_column("entity_catalog_snapshot", "source_type")
    op.alter_column(
        "entity_catalog_snapshot", "source_locator", new_column_name="source_url"
    )

    op.alter_column(
        "ontology_metadata", "source_revision", existing_type=sa.Text(), nullable=False
    )
    op.alter_column(
        "ontology_metadata", "refresh_result", new_column_name="load_result"
    )
    op.alter_column("ontology_metadata", "fetched_at", new_column_name="loaded_at")

    op.drop_table("authorization_refresh")
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
        sa.UniqueConstraint(
            "source_repository", "source_commit_sha", name="source_revision_unique"
        ),
    )

    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        "job_type IN ('authorization_sync', 'entity_catalog_retirement', "
        "'entity_import', 'ontology_load')",
    )
