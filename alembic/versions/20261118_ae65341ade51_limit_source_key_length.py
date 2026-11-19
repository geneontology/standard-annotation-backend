"""Limit source keys to 64 characters.

Replaces the `source_key_format` checks on `reference_snapshot` and
`annotation_import` with the pattern `SOURCE_KEY_PATTERN` now gives, which also
limits a key's length. Every configured key already fits, so no rows change.

Downgrading restores the checks without the length limit.

Revision ID: ae65341ade51
Revises: 7c713adb5200
Create Date: 2026-11-18 15:27:16.323547
"""

from collections.abc import Sequence

from alembic import op

revision: str = "ae65341ade51"
down_revision: str | None = "7c713adb5200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("reference_snapshot", "annotation_import")
_LIMITED = "source_key ~ '^[a-z0-9][a-z0-9_-]{0,63}$'"
_UNLIMITED = "source_key ~ '^[a-z0-9][a-z0-9_-]*$'"


def _replace_checks(condition: str) -> None:
    """Replace each table's `source_key_format` check with `condition`."""
    for table in _TABLES:
        name = op.f(f"ck_{table}_source_key_format")
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    """Limit source keys to 64 characters."""
    _replace_checks(_LIMITED)


def downgrade() -> None:
    """Restore the source key checks without a length limit."""
    _replace_checks(_UNLIMITED)
