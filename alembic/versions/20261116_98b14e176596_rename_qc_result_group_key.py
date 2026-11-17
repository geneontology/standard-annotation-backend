"""Rename `group_key` to `owning_group_id` in stored QC report results.

QC report results now name the group a report covered `owning_group_id`, as the
report API does. Job parameters keep `group_key`.

Revision ID: 98b14e176596
Revises: 1734aa3abf25
Create Date: 2026-11-16 16:01:55.614207
"""

from collections.abc import Sequence

from alembic import op

revision: str = "98b14e176596"
down_revision: str | None = "1734aa3abf25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename the key in every stored QC report result."""
    op.execute(
        "UPDATE job SET result = (result - 'group_key') "
        "|| jsonb_build_object('owning_group_id', result -> 'group_key') "
        "WHERE job_type = 'annotation_qc_report' AND result ? 'group_key'"
    )


def downgrade() -> None:
    """Restore the earlier key in every stored QC report result."""
    op.execute(
        "UPDATE job SET result = (result - 'owning_group_id') "
        "|| jsonb_build_object('group_key', result -> 'owning_group_id') "
        "WHERE job_type = 'annotation_qc_report' AND result ? 'owning_group_id'"
    )
