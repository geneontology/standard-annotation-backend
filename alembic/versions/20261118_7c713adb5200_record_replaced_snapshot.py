"""Record which snapshot each activated snapshot replaced.

Adds `reference_snapshot.replaced_snapshot_id`, the snapshot that was active for the
same source when this one was activated. Pruning keeps the snapshot the active one
replaced, so it no longer compares activation times, which come from each worker's
clock. Existing snapshots get no link; the next activation of each source sets one.

Downgrading drops the column and its foreign key.

Revision ID: 7c713adb5200
Revises: 10339c3945da
Create Date: 2026-11-18 13:00:44.147184
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7c713adb5200"
down_revision: str | None = "10339c3945da"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `replaced_snapshot_id` with a foreign key to `reference_snapshot`."""
    op.add_column(
        "reference_snapshot",
        sa.Column("replaced_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_reference_snapshot_replaced_snapshot_id_reference_snapshot",
        "reference_snapshot",
        "reference_snapshot",
        ["replaced_snapshot_id"],
        ["snapshot_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Drop `replaced_snapshot_id` and its foreign key."""
    op.drop_constraint(
        "fk_reference_snapshot_replaced_snapshot_id_reference_snapshot",
        "reference_snapshot",
        type_="foreignkey",
    )
    op.drop_column("reference_snapshot", "replaced_snapshot_id")
