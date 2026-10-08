"""Remove the unused job artifact URI.

No job ever recorded an artifact, so the column and the public `artifact_uri`
job field are removed. Downgrading restores the empty, nullable column.

Revision ID: e3e60c292b7b
Revises: b0021e43fa16
Create Date: 2026-10-06 23:56:32.740296
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3e60c292b7b"
down_revision: str | None = "b0021e43fa16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision."""
    op.drop_column("job", "artifact_uri")


def downgrade() -> None:
    """Revert this revision."""
    op.add_column(
        "job", sa.Column("artifact_uri", sa.TEXT(), autoincrement=False, nullable=True)
    )
