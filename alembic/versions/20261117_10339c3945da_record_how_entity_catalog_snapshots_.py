"""Record how entity catalog snapshots were read.

Makes three changes to `entity_snapshot_detail`:

- Drops `record_statistics`, which nothing reads. The job that activated a snapshot
  already records its counts and rejected rows.
- Adds `rejected_collisions`, whether activation rejected any row because another
  source's active catalog lists its identifier. A collision depends on that catalog
  rather than on the source's file, so when it is true a refresh of the same file
  reads it again. Existing snapshots are marked as having rejected none, because
  earlier refreshes failed instead of rejecting collisions.
- Adds `schema_version`, the `go-standard-annotation-schema` version that checked the
  snapshot's rows. A different version can accept or reject different rows, so when
  it differs from the installed version a refresh of the same file reads it again.
  Existing snapshots get an empty version, which matches no installed package, so
  the next refresh reads each of them again.

Downgrading drops the two new columns and restores `record_statistics` with an empty
object in every row.

Revision ID: 10339c3945da
Revises: 98b14e176596
Create Date: 2026-11-17 17:16:40.127055
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "10339c3945da"
down_revision: str | None = "98b14e176596"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop `record_statistics`; add `rejected_collisions` and `schema_version`."""
    op.drop_column("entity_snapshot_detail", "record_statistics")
    op.add_column(
        "entity_snapshot_detail",
        sa.Column(
            "rejected_collisions",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column(
        "entity_snapshot_detail", "rejected_collisions", server_default=None
    )
    op.add_column(
        "entity_snapshot_detail",
        sa.Column("schema_version", sa.Text(), nullable=False, server_default=""),
    )
    op.alter_column("entity_snapshot_detail", "schema_version", server_default=None)


def downgrade() -> None:
    """Drop the new columns and restore `record_statistics` with `{}` in each row."""
    op.drop_column("entity_snapshot_detail", "schema_version")
    op.drop_column("entity_snapshot_detail", "rejected_collisions")
    op.add_column(
        "entity_snapshot_detail",
        sa.Column(
            "record_statistics",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("entity_snapshot_detail", "record_statistics", server_default=None)
    op.create_check_constraint(
        op.f("ck_entity_snapshot_detail_record_statistics_object"),
        "entity_snapshot_detail",
        "jsonb_typeof(record_statistics) = 'object'",
    )
