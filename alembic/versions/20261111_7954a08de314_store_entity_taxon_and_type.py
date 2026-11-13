"""Store each entity source record's taxon and type in their own columns.

Adds `db_object_taxon_id` and `db_object_type` to `entity_source_record`, so
validation can read them without parsing the stored `entity` JSON. Fills them from
each record's `entity` and adds a check that keeps them equal to it. Downgrading
drops the check and the columns.

Revision ID: 7954a08de314
Revises: 1d87c729df70
Create Date: 2026-11-11 13:21:41.340716
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7954a08de314"
down_revision: str | None = "1d87c729df70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    op.add_column(
        "entity_source_record",
        sa.Column("db_object_taxon_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "entity_source_record",
        sa.Column("db_object_type", sa.Text(), nullable=True),
    )
    op.execute(
        "UPDATE entity_source_record SET "
        "db_object_taxon_id = entity->>'db_object_taxon_id', "
        "db_object_type = entity->>'db_object_type'"
    )
    op.alter_column("entity_source_record", "db_object_taxon_id", nullable=False)
    op.alter_column("entity_source_record", "db_object_type", nullable=False)
    op.create_check_constraint(
        op.f("ck_entity_source_record_taxon_and_type_match_entity"),
        "entity_source_record",
        "db_object_taxon_id IS NOT DISTINCT FROM entity->>'db_object_taxon_id' "
        "AND db_object_type IS NOT DISTINCT FROM entity->>'db_object_type'",
    )


def downgrade() -> None:
    """Revert this revision."""
    op.drop_constraint(
        op.f("ck_entity_source_record_taxon_and_type_match_entity"),
        "entity_source_record",
        type_="check",
    )
    op.drop_column("entity_source_record", "db_object_type")
    op.drop_column("entity_source_record", "db_object_taxon_id")
