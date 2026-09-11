"""Protect the database safety check used by integration-test setup.

This is intentionally a test of test infrastructure, not application behavior.
The integration fixture runs destructive migrations, so a regression in its URL
validation could reset a developer's non-test database. Keep this test while that
fixture is responsible for resetting the configured database.
"""

from importlib import import_module
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "query",
    (
        "dbname=sab",
        "db%6Eame=sab",
        "dbname=sab_test&dbname=sab",
    ),
)
def test_database_guard_rejects_query_database_override_before_setup(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    """Reject database overrides before integration setup can connect or migrate."""
    # Import the real fixture guard so this test cannot drift into checking a copy.
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    test_database_url = import_module("integration.conftest")._test_database_url
    monkeypatch.setenv("SAB_ENVIRONMENT", "testing")
    monkeypatch.setenv(
        "SAB_TEST_DATABASE_URL",
        f"postgresql+psycopg://sab:sab@localhost:5432/sab_test?{query}",
    )

    # No database fixture is used: validation must fail before setup reaches the
    # connection and migration steps.
    with pytest.raises(pytest.UsageError, match=r"dbname.*overrides"):
        test_database_url()
