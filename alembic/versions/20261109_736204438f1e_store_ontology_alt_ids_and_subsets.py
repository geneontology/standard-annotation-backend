"""Store ontology alternative identifiers and term subsets.

Each term now records the ontology subsets it belongs to, and a new table maps each
alternative identifier to the term it names in the same snapshot. Each snapshot also
records the identifier prefixes it was read with, so that changing an ontology's
prefixes or closure predicates loads an unchanged file again. Existing snapshots
record no prefixes, so the next refresh of each ontology loads it again with subsets
and alternative identifiers. Downgrading drops the table and the columns.

Revision ID: 736204438f1e
Revises: 184f7be9fc74
Create Date: 2026-11-09 11:36:07.821389
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "736204438f1e"
down_revision: str | None = "184f7be9fc74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    op.add_column(
        "ontology_term",
        sa.Column(
            "subsets",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("ontology_term", "subsets", server_default=None)
    op.create_check_constraint(
        op.f("ck_ontology_term_subsets_string_array"),
        "ontology_term",
        "jsonb_typeof(subsets) = 'array' AND NOT "
        "jsonb_path_exists(subsets, '$[*] ? (@.type() != \"string\")')",
    )
    op.add_column(
        "ontology_metadata",
        sa.Column(
            "identifier_prefixes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("ontology_metadata", "identifier_prefixes", server_default=None)
    op.create_check_constraint(
        op.f("ck_ontology_metadata_identifier_prefixes_string_array"),
        "ontology_metadata",
        "jsonb_typeof(identifier_prefixes) = 'array' AND NOT "
        "jsonb_path_exists(identifier_prefixes, '$[*] ? (@.type() != \"string\")')",
    )
    op.create_table(
        "ontology_alt_id",
        sa.Column("version_id", sa.UUID(), nullable=False),
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


def downgrade() -> None:
    """Revert this revision."""
    op.drop_table("ontology_alt_id")
    op.drop_constraint(
        op.f("ck_ontology_metadata_identifier_prefixes_string_array"),
        "ontology_metadata",
        type_="check",
    )
    op.drop_column("ontology_metadata", "identifier_prefixes")
    op.drop_constraint(
        op.f("ck_ontology_term_subsets_string_array"), "ontology_term", type_="check"
    )
    op.drop_column("ontology_term", "subsets")
