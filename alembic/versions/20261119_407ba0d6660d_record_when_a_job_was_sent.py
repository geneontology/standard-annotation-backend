"""Record when a job was first sent to a worker.

Adds the nullable `sent_at` column to `job`. `JobService.send` sets it when a send
succeeds, and fails a job whose send raised only while it is still empty, so a
caller whose send fails no longer fails a job that another caller already put in
the broker. Existing jobs keep it empty.

Downgrading drops the column.

Revision ID: 407ba0d6660d
Revises: ae65341ade51
Create Date: 2026-11-19 10:35:43.853371
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "407ba0d6660d"
down_revision: str | None = "ae65341ade51"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add `job.sent_at`."""
    op.add_column("job", sa.Column("sent_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    """Drop `job.sent_at`."""
    op.drop_column("job", "sent_at")
