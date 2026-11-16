"""Remove entity retirement jobs; refreshing every source retires sources directly.

Deletes `entity_retirement` jobs and stops the `job` table from accepting the
type. The previous revision already deleted the catalogs they retired; their
audit events are kept. Downgrading accepts the type again but cannot restore
the deleted jobs.

Revision ID: 1734aa3abf25
Revises: cd3f90c041ed
Create Date: 2026-11-13 18:18:30.431098
"""

from collections.abc import Sequence

from alembic import op

revision: str = "1734aa3abf25"
down_revision: str | None = "cd3f90c041ed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOB_TYPES = (
    "'annotation_cutover', 'annotation_qc_report', 'annotation_refresh', "
    "'authorization_refresh', 'entity_refresh', 'ontology_refresh', "
    "'rules_refresh', 'term_replacement'"
)


def upgrade() -> None:
    """Delete entity retirement jobs and stop accepting the job type."""
    op.execute("DELETE FROM job WHERE job_type = 'entity_retirement'")
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"), "job", f"job_type IN ({_JOB_TYPES})"
    )


def downgrade() -> None:
    """Accept entity retirement jobs again."""
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        f"job_type IN ({_JOB_TYPES}, 'entity_retirement')",
    )
