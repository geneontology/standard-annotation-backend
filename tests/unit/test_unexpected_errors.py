"""Tests for responses to failures that SAB does not expect."""

import logging

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from standard_annotation_backend.api.dependencies import get_unit_of_work_factory
from standard_annotation_backend.main import app

_SECRET = "secret-value"


def _fail() -> None:
    raise RuntimeError(_SECRET)


def test_unexpected_failure_returns_internal_error_and_logs_only_its_type(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected failure returns the JSON envelope and logs no failure text."""
    # Migration tests load Alembic's logging file, which disables loggers that
    # already exist in the test process.
    monkeypatch.setattr(
        logging.getLogger("standard_annotation_backend.main"), "disabled", False
    )
    monkeypatch.setitem(app.dependency_overrides, get_unit_of_work_factory, _fail)
    with caplog.at_level(logging.DEBUG):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/annotations/00000000-0000-0000-0000-000000000001",
            params={"note": "query-secret"},
        )

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "An unexpected error occurred",
        }
    }
    sab_records = [
        record
        for record in caplog.records
        if record.name.startswith("standard_annotation_backend")
    ]
    assert any("RuntimeError" in record.getMessage() for record in sab_records)
    assert all("query-secret" not in record.getMessage() for record in sab_records)
    for record in caplog.records:
        assert _SECRET not in f"{record.getMessage()} {record.args!r}"
        assert record.exc_info is None
