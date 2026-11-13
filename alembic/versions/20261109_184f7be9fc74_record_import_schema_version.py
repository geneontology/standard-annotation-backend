"""Record the schema version on annotation imports.

Each GPAD import now records the version of the `go-standard-annotation-schema`
package its rows were checked with, so an unchanged file is imported again after a
schema upgrade. Existing imports get `unknown`, which matches no real version, so
the next refresh of each group imports its file again. Downgrading drops the column.

Until a group is imported again, reading any of its stored annotations that the new
schema rejects fails. Refresh every annotation source after upgrading.

Revision ID: 184f7be9fc74
Revises: 11a0f613e2f8
Create Date: 2026-11-09 09:03:15.211523
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "184f7be9fc74"
down_revision: str | None = "11a0f613e2f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    op.add_column(
        "annotation_import",
        sa.Column(
            "schema_version", sa.Text(), nullable=False, server_default="unknown"
        ),
    )
    op.alter_column("annotation_import", "schema_version", server_default=None)


def downgrade() -> None:
    """Revert this revision."""
    op.drop_column("annotation_import", "schema_version")
