"""Shared pytest fixtures for the SAB test suite."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from standard_annotation_backend.config import get_settings
from standard_annotation_backend.main import app


@pytest.fixture
def configured_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide complete test configuration, clearing cached settings between tests."""
    monkeypatch.setenv("SAB_DATABASE_URL", "postgresql+psycopg://sab:sab@db:5432/sab")
    monkeypatch.setenv("SAB_REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("SAB_APPLICATION_SECRET", "test-application-secret")
    monkeypatch.setenv("SAB_ENVIRONMENT", "test")
    monkeypatch.setenv("SAB_LOG_FORMAT", "console")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def client(configured_environment: None) -> Iterator[TestClient]:
    """Run the FastAPI application with valid test configuration."""
    with TestClient(app) as test_client:
        yield test_client
