"""add ontology retention state

Revision ID: b971c66386e0
Revises: 46874af4d8ce
Create Date: 2026-09-29 17:43:46.598556
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b971c66386e0"
down_revision: str | None = "46874af4d8ce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the pruning time and prevent active snapshots from being marked pruned."""
    op.add_column(
        "ontology_metadata",
        sa.Column("bulk_data_pruned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_ontology_metadata_active_bulk_data_complete"),
        "ontology_metadata",
        "NOT active OR bulk_data_pruned_at IS NULL",
    )


def downgrade() -> None:
    """Remove the pruning time and active-snapshot constraint."""
    op.drop_constraint(
        op.f("ck_ontology_metadata_active_bulk_data_complete"),
        "ontology_metadata",
        type_="check",
    )
    op.drop_column("ontology_metadata", "bulk_data_pruned_at")
