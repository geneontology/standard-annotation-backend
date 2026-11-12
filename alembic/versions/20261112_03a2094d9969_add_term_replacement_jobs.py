"""Allow term replacement jobs and rename the automatic-replacement change source.

Adds `term_replacement` to the job types the `job` table accepts, and renames the
stored change source `ontology_refresh` to `term_replacement` on annotation
versions and in audit event details. Downgrading reverses the rename and deletes
term replacement jobs and their audit events.

Revision ID: 03a2094d9969
Revises: 7954a08de314
Create Date: 2026-11-12 10:18:36.790378
"""

from collections.abc import Sequence

from alembic import op

revision: str = "03a2094d9969"
down_revision: str | None = "7954a08de314"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JOB_TYPES = (
    "'annotation_cutover', 'annotation_refresh', 'authorization_refresh', "
    "'entity_refresh', 'entity_retirement', 'ontology_refresh', 'rules_refresh'"
)


def upgrade() -> None:
    """Allow term replacement jobs and rename the automatic-replacement source."""
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"),
        "job",
        f"job_type IN ({_JOB_TYPES}, 'term_replacement')",
    )
    op.execute(
        "UPDATE annotation_version SET change_source = 'term_replacement' "
        "WHERE change_source = 'ontology_refresh'"
    )
    op.execute(
        "UPDATE audit_event SET details = jsonb_set(details, '{change_source}', "
        "'\"term_replacement\"') WHERE details->>'change_source' = 'ontology_refresh'"
    )


def downgrade() -> None:
    """Remove term replacement jobs and restore the earlier source name."""
    op.execute(
        "UPDATE audit_event SET details = jsonb_set(details, '{change_source}', "
        "'\"ontology_refresh\"') WHERE details->>'change_source' = 'term_replacement'"
    )
    op.execute(
        "UPDATE annotation_version SET change_source = 'ontology_refresh' "
        "WHERE change_source = 'term_replacement'"
    )
    op.execute(
        "DELETE FROM audit_event WHERE job_id IN "
        "(SELECT job_id FROM job WHERE job_type = 'term_replacement')"
    )
    op.execute("DELETE FROM job WHERE job_type = 'term_replacement'")
    op.drop_constraint(op.f("ck_job_type_allowed"), "job", type_="check")
    op.create_check_constraint(
        op.f("ck_job_type_allowed"), "job", f"job_type IN ({_JOB_TYPES})"
    )
