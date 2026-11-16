"""Store every reference data snapshot in one table and limit unfinished jobs.

Replaces `ontology_metadata`, `rules_snapshot`, and `entity_catalog_snapshot`
with `reference_snapshot`, three detail tables, and data tables keyed by
`snapshot_id`: `ontology_term`, `ontology_alt_id`, `ontology_closure`,
`rule_data`, `retracted_publication`, and `entity_record`, which replaces
`entity_membership`, `entity_source_record`, and `entity_staging_record`.
Stored ontologies, rule data, and entity catalogs are deleted, so every kind
must be refreshed after upgrading; job history is kept. Also adds
`reader_version` to `authorization_refresh`, keeps only published GPAD imports,
and allows at most one unfinished refresh job per source and one unfinished
GPAD job per group.

The upgrade refuses to run while any refresh, retirement, or GPAD job is queued
or running. Downgrading restores the previous tables empty.

Revision ID: cd3f90c041ed
Revises: 1becb2618dc3
Create Date: 2026-11-13 17:35:22.615597
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "cd3f90c041ed"
down_revision: str | None = "1becb2618dc3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARDED_JOB_TYPES = (
    "'annotation_cutover', 'annotation_refresh', 'authorization_refresh', "
    "'entity_refresh', 'entity_retirement', 'ontology_refresh', 'rules_refresh'"
)
_STRING_ARRAY = (
    "jsonb_typeof({0}) = 'array' AND NOT jsonb_path_exists({0}, "
    "'$[*] ? (@.type() != \"string\")')"
)
_TAXON_AND_TYPE = (
    "db_object_taxon_id IS NOT DISTINCT FROM entity->>'db_object_taxon_id' "
    "AND db_object_type IS NOT DISTINCT FROM entity->>'db_object_type'"
)


def _uuid(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), nullable=nullable)


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    """Replace the snapshot tables, then add the reader version and job limits."""
    unfinished = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM job WHERE status IN ('queued', 'running') "
            f"AND job_type IN ({_GUARDED_JOB_TYPES})"
        )
    )
    if unfinished:
        raise RuntimeError(
            "Refresh, retirement, or GPAD jobs are still queued or running. Stop "
            "the scheduler and wait for the workers to finish them, or start the "
            "same refresh again to finish a stuck job, then upgrade"
        )
    for table in (
        "ontology_closure",
        "ontology_alt_id",
        "ontology_term",
        "ontology_metadata",
        "retracted_publication",
        "rules_snapshot",
        "entity_source_record",
        "entity_membership",
        "entity_staging_record",
        "entity_catalog_snapshot",
    ):
        op.drop_table(table)
    _create_snapshot_tables()

    op.add_column(
        "authorization_refresh",
        sa.Column("reader_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.alter_column("authorization_refresh", "reader_version", server_default=None)
    op.create_check_constraint(
        op.f("ck_authorization_refresh_reader_version_nonnegative"),
        "authorization_refresh",
        "reader_version >= 0",
    )

    op.execute("DELETE FROM annotation_staging_duplicate_reference")
    op.execute("DELETE FROM annotation_staging_multivalued_value")
    op.execute("DELETE FROM annotation_staging")
    op.execute("DELETE FROM annotation_import WHERE published_at IS NULL")
    op.drop_constraint(
        op.f("ck_annotation_import_publication_complete"),
        "annotation_import",
        type_="check",
    )
    op.alter_column(
        "annotation_import",
        "published_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.alter_column(
        "annotation_import",
        "annotations_deleted",
        existing_type=sa.BigInteger(),
        nullable=False,
    )

    op.create_index(
        "uq_job_unfinished_refresh_source",
        "job",
        ["job_type", sa.text("(parameters ->> 'source_key')")],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'running') AND job_type IN "
            "('authorization_refresh', 'entity_refresh', 'ontology_refresh', "
            "'rules_refresh')"
        ),
    )
    op.create_index(
        "uq_job_unfinished_gpad_group",
        "job",
        [sa.text("(parameters ->> 'source_key')")],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'running') AND job_type IN "
            "('annotation_cutover', 'annotation_refresh')"
        ),
    )


def _create_snapshot_tables() -> None:
    """Create `reference_snapshot`, its detail tables, and its data tables."""
    op.create_table(
        "reference_snapshot",
        _uuid("snapshot_id"),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        _uuid("job_id"),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=True),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        _timestamp("fetched_at"),
        sa.Column("reader_version", sa.Integer(), nullable=False),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        _timestamp("activated_at", nullable=True),
        _timestamp("pruned_at", nullable=True),
        _timestamp("retired_at", nullable=True),
        sa.CheckConstraint(
            "kind IN ('entity', 'ontology', 'rules')",
            name=op.f("ck_reference_snapshot_kind_allowed"),
        ),
        sa.CheckConstraint(
            "source_key ~ '^[a-z0-9][a-z0-9_-]*$'",
            name=op.f("ck_reference_snapshot_source_key_format"),
        ),
        sa.CheckConstraint(
            "source_checksum ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_reference_snapshot_source_checksum_format"),
        ),
        sa.CheckConstraint(
            "reader_version >= 0",
            name=op.f("ck_reference_snapshot_reader_version_nonnegative"),
        ),
        sa.CheckConstraint(
            "NOT active OR (activated_at IS NOT NULL AND pruned_at IS NULL "
            "AND retired_at IS NULL)",
            name=op.f("ck_reference_snapshot_active_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["job.job_id"],
            name=op.f("fk_reference_snapshot_job_id_job"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_reference_snapshot")),
        sa.UniqueConstraint("job_id", name=op.f("uq_reference_snapshot_job_id")),
    )
    op.create_index(
        "uq_reference_snapshot_active_source",
        "reference_snapshot",
        ["kind", "source_key"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_index(
        "ix_reference_snapshot_kind_source_activated",
        "reference_snapshot",
        ["kind", "source_key", "activated_at"],
    )

    def snapshot_key(table: str) -> sa.ForeignKeyConstraint:
        return sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["reference_snapshot.snapshot_id"],
            name=op.f(f"fk_{table}_snapshot_id_reference_snapshot"),
            ondelete="CASCADE",
        )

    op.create_table(
        "ontology_snapshot_detail",
        _uuid("snapshot_id"),
        sa.Column("document_version", sa.Text(), nullable=True),
        sa.Column("loaded_predicates", postgresql.JSONB(), nullable=False),
        sa.Column("identifier_prefixes", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            _STRING_ARRAY.format("loaded_predicates"),
            name=op.f("ck_ontology_snapshot_detail_loaded_predicates_string_array"),
        ),
        sa.CheckConstraint(
            _STRING_ARRAY.format("identifier_prefixes"),
            name=op.f("ck_ontology_snapshot_detail_identifier_prefixes_string_array"),
        ),
        snapshot_key("ontology_snapshot_detail"),
        sa.PrimaryKeyConstraint(
            "snapshot_id", name=op.f("pk_ontology_snapshot_detail")
        ),
    )
    op.create_table(
        "rules_snapshot_detail",
        _uuid("snapshot_id"),
        sa.Column("retracted_publication_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "retracted_publication_count >= 0",
            name=op.f(
                "ck_rules_snapshot_detail_retracted_publication_count_nonnegative"
            ),
        ),
        snapshot_key("rules_snapshot_detail"),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_rules_snapshot_detail")),
    )
    op.create_table(
        "entity_snapshot_detail",
        _uuid("snapshot_id"),
        sa.Column("source_format", sa.String(length=20), nullable=False),
        sa.Column("source_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("record_statistics", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(source_metadata) = 'object'",
            name=op.f("ck_entity_snapshot_detail_source_metadata_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(record_statistics) = 'object'",
            name=op.f("ck_entity_snapshot_detail_record_statistics_object"),
        ),
        snapshot_key("entity_snapshot_detail"),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_entity_snapshot_detail")),
    )
    op.create_table(
        "ontology_term",
        _uuid("snapshot_id"),
        sa.Column("term_id", sa.Text(), nullable=False),
        sa.Column("obsolete", sa.Boolean(), nullable=False),
        sa.Column("replaced_by", postgresql.JSONB(), nullable=False),
        sa.Column("consider", postgresql.JSONB(), nullable=False),
        sa.Column("subsets", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            _STRING_ARRAY.format("replaced_by"),
            name=op.f("ck_ontology_term_replaced_by_string_array"),
        ),
        sa.CheckConstraint(
            _STRING_ARRAY.format("consider"),
            name=op.f("ck_ontology_term_consider_string_array"),
        ),
        sa.CheckConstraint(
            _STRING_ARRAY.format("subsets"),
            name=op.f("ck_ontology_term_subsets_string_array"),
        ),
        snapshot_key("ontology_term"),
        sa.PrimaryKeyConstraint(
            "snapshot_id", "term_id", name=op.f("pk_ontology_term")
        ),
    )
    op.create_table(
        "ontology_alt_id",
        _uuid("snapshot_id"),
        sa.Column("alt_id", sa.Text(), nullable=False),
        sa.Column("term_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "term_id"],
            ["ontology_term.snapshot_id", "ontology_term.term_id"],
            name=op.f("fk_ontology_alt_id_snapshot_id_ontology_term"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_id", "alt_id", name=op.f("pk_ontology_alt_id")
        ),
    )
    op.create_index(
        "ix_ontology_alt_id_term", "ontology_alt_id", ["snapshot_id", "term_id"]
    )
    op.create_table(
        "ontology_closure",
        _uuid("snapshot_id"),
        sa.Column("subject_term_id", sa.Text(), nullable=False),
        sa.Column("predicate_id", sa.Text(), nullable=False),
        sa.Column("object_term_id", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "depth >= 0", name=op.f("ck_ontology_closure_depth_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "subject_term_id"],
            ["ontology_term.snapshot_id", "ontology_term.term_id"],
            name="fk_ontology_closure_subject",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "object_term_id"],
            ["ontology_term.snapshot_id", "ontology_term.term_id"],
            name="fk_ontology_closure_object",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_id",
            "subject_term_id",
            "predicate_id",
            "object_term_id",
            name=op.f("pk_ontology_closure"),
        ),
    )
    op.create_index(
        "ix_ontology_closure_object_predicate",
        "ontology_closure",
        ["snapshot_id", "object_term_id", "predicate_id"],
    )
    op.create_table(
        "rule_data",
        _uuid("snapshot_id"),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(data) = 'object'", name=op.f("ck_rule_data_data_object")
        ),
        snapshot_key("rule_data"),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_rule_data")),
    )
    op.create_table(
        "retracted_publication",
        _uuid("snapshot_id"),
        sa.Column("pmid", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "pmid ~ '^PMID:[0-9]+$'", name=op.f("ck_retracted_publication_pmid_format")
        ),
        snapshot_key("retracted_publication"),
        sa.PrimaryKeyConstraint(
            "snapshot_id", "pmid", name=op.f("pk_retracted_publication")
        ),
    )
    op.create_table(
        "entity_record",
        _uuid("snapshot_id"),
        sa.Column("db_object_id", sa.Text(), nullable=False),
        sa.Column("line_number", sa.BigInteger(), nullable=False),
        sa.Column("entity", postgresql.JSONB(), nullable=False),
        sa.Column("db_object_taxon_id", sa.Text(), nullable=False),
        sa.Column("db_object_type", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "line_number > 0", name=op.f("ck_entity_record_line_number_positive")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(entity) = 'object'",
            name=op.f("ck_entity_record_entity_object"),
        ),
        sa.CheckConstraint(
            _TAXON_AND_TYPE, name=op.f("ck_entity_record_taxon_and_type_match_entity")
        ),
        snapshot_key("entity_record"),
        sa.PrimaryKeyConstraint(
            "snapshot_id", "db_object_id", name=op.f("pk_entity_record")
        ),
    )
    op.create_index("ix_entity_record_db_object_id", "entity_record", ["db_object_id"])


def downgrade() -> None:
    """Restore the previous tables, empty, and remove the reader version and limits."""
    op.drop_index("uq_job_unfinished_gpad_group", table_name="job")
    op.drop_index("uq_job_unfinished_refresh_source", table_name="job")
    op.alter_column(
        "annotation_import",
        "annotations_deleted",
        existing_type=sa.BigInteger(),
        nullable=True,
    )
    op.alter_column(
        "annotation_import",
        "published_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    op.create_check_constraint(
        op.f("ck_annotation_import_publication_complete"),
        "annotation_import",
        "(published_at IS NULL) = (annotations_deleted IS NULL)",
    )
    op.drop_constraint(
        op.f("ck_authorization_refresh_reader_version_nonnegative"),
        "authorization_refresh",
        type_="check",
    )
    op.drop_column("authorization_refresh", "reader_version")
    for table in (
        "entity_record",
        "retracted_publication",
        "rule_data",
        "ontology_closure",
        "ontology_alt_id",
        "ontology_term",
        "entity_snapshot_detail",
        "rules_snapshot_detail",
        "ontology_snapshot_detail",
        "reference_snapshot",
    ):
        op.drop_table(table)
    _create_previous_tables()


def _create_previous_tables() -> None:
    """Create the snapshot tables of revision `1becb2618dc3`, empty."""
    op.create_table(
        "ontology_metadata",
        _uuid("version_id"),
        sa.Column(
            "staging_sequence",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("ontology_key", sa.String(length=100), nullable=False),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=True),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("document_version", sa.Text(), nullable=True),
        sa.Column("loaded_predicates", postgresql.JSONB(), nullable=False),
        sa.Column("identifier_prefixes", postgresql.JSONB(), nullable=False),
        sa.Column("refresh_result", postgresql.JSONB(), nullable=True),
        _uuid("job_id"),
        _timestamp("fetched_at"),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        _timestamp("bulk_data_pruned_at", nullable=True),
        sa.CheckConstraint(
            "source_checksum ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_ontology_metadata_source_checksum_format"),
        ),
        sa.CheckConstraint(
            _STRING_ARRAY.format("loaded_predicates"),
            name=op.f("ck_ontology_metadata_loaded_predicates_string_array"),
        ),
        sa.CheckConstraint(
            _STRING_ARRAY.format("identifier_prefixes"),
            name=op.f("ck_ontology_metadata_identifier_prefixes_string_array"),
        ),
        sa.CheckConstraint(
            "NOT active OR bulk_data_pruned_at IS NULL",
            name=op.f("ck_ontology_metadata_active_bulk_data_complete"),
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
            "staging_sequence", name=op.f("uq_ontology_metadata_staging_sequence")
        ),
    )
    op.create_index(
        "uq_ontology_metadata_active_key",
        "ontology_metadata",
        ["ontology_key"],
        unique=True,
        postgresql_where=sa.text("active"),
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
    )
    op.create_table(
        "ontology_term",
        _uuid("version_id"),
        sa.Column("term_id", sa.Text(), nullable=False),
        sa.Column("obsolete", sa.Boolean(), nullable=False),
        sa.Column("replaced_by", postgresql.JSONB(), nullable=False),
        sa.Column("consider", postgresql.JSONB(), nullable=False),
        sa.Column("subsets", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            _STRING_ARRAY.format("replaced_by"),
            name=op.f("ck_ontology_term_replaced_by_string_array"),
        ),
        sa.CheckConstraint(
            _STRING_ARRAY.format("consider"),
            name=op.f("ck_ontology_term_consider_string_array"),
        ),
        sa.CheckConstraint(
            _STRING_ARRAY.format("subsets"),
            name=op.f("ck_ontology_term_subsets_string_array"),
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
        "ontology_alt_id",
        _uuid("version_id"),
        sa.Column("alt_id", sa.Text(), nullable=False),
        sa.Column("term_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id", "term_id"],
            ["ontology_term.version_id", "ontology_term.term_id"],
            name=op.f("fk_ontology_alt_id_version_id_ontology_term"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "version_id", "alt_id", name=op.f("pk_ontology_alt_id")
        ),
    )
    op.create_index(
        "ix_ontology_alt_id_term", "ontology_alt_id", ["version_id", "term_id"]
    )
    op.create_table(
        "ontology_closure",
        _uuid("version_id"),
        sa.Column("subject_term_id", sa.Text(), nullable=False),
        sa.Column("predicate_id", sa.Text(), nullable=False),
        sa.Column("object_term_id", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "depth >= 0", name=op.f("ck_ontology_closure_depth_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "subject_term_id"],
            ["ontology_term.version_id", "ontology_term.term_id"],
            name="fk_ontology_closure_subject",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "object_term_id"],
            ["ontology_term.version_id", "ontology_term.term_id"],
            name="fk_ontology_closure_object",
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
    )
    op.create_table(
        "rules_snapshot",
        _uuid("snapshot_id"),
        _uuid("job_id"),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=True),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        _timestamp("fetched_at"),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("rule_data", postgresql.JSONB(), nullable=False),
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
        _uuid("snapshot_id"),
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
    op.create_table(
        "entity_catalog_snapshot",
        _uuid("snapshot_id"),
        _uuid("job_id"),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=True),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("source_format", sa.String(length=20), nullable=False),
        sa.Column("source_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("source_statistics", postgresql.JSONB(), nullable=False),
        sa.Column("record_statistics", postgresql.JSONB(), nullable=False),
        sa.Column("publication_result", postgresql.JSONB(), nullable=True),
        _timestamp("fetched_at"),
        _timestamp("staged_at"),
        _timestamp("published_at", nullable=True),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        _timestamp("retired_at", nullable=True),
        _uuid("retired_by_job_id", nullable=True),
        sa.Column("retirement_result", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint(
            "source_checksum ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_entity_catalog_snapshot_source_checksum_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_metadata) = 'object'",
            name=op.f("ck_entity_catalog_snapshot_source_metadata_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_statistics) = 'object'",
            name=op.f("ck_entity_catalog_snapshot_source_statistics_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(record_statistics) = 'object'",
            name=op.f("ck_entity_catalog_snapshot_record_statistics_object"),
        ),
        sa.CheckConstraint(
            "publication_result IS NULL OR jsonb_typeof(publication_result) = 'object'",
            name=op.f("ck_entity_catalog_snapshot_publication_result_object"),
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
        _uuid("job_id"),
        sa.Column("line_number", sa.BigInteger(), nullable=False),
        sa.Column("db_object_id", sa.Text(), nullable=False),
        sa.Column("entity", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "line_number > 0",
            name=op.f("ck_entity_staging_record_line_number_positive"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(entity) = 'object'",
            name=op.f("ck_entity_staging_record_entity_object"),
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
    )
    op.create_table(
        "entity_membership",
        sa.Column("db_object_id", sa.Text(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        _uuid("snapshot_id"),
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
        "ix_entity_membership_source_key", "entity_membership", ["source_key"]
    )
    op.create_table(
        "entity_source_record",
        sa.Column("db_object_id", sa.Text(), nullable=False),
        sa.Column("line_number", sa.BigInteger(), nullable=False),
        sa.Column("entity", postgresql.JSONB(), nullable=False),
        sa.Column("db_object_taxon_id", sa.Text(), nullable=False),
        sa.Column("db_object_type", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "line_number > 0",
            name=op.f("ck_entity_source_record_line_number_positive"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(entity) = 'object'",
            name=op.f("ck_entity_source_record_entity_object"),
        ),
        sa.CheckConstraint(
            _TAXON_AND_TYPE,
            name=op.f("ck_entity_source_record_taxon_and_type_match_entity"),
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
