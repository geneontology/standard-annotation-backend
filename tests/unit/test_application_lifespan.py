import pytest
from fastapi import status
from fastapi.testclient import TestClient

import standard_annotation_backend.main as main_module


class _ConnectionlessEngine:
    """Record cleanup and fail if application startup attempts a connection."""

    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_lifespan_starts_without_connecting_and_disposes_engine(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup opens no database connection and shutdown disposes the engine."""
    engine = _ConnectionlessEngine()
    monkeypatch.setattr(
        main_module,
        "create_database_engine",
        lambda _url: engine,
        raising=False,
    )

    with TestClient(main_module.app) as local_client:
        response = local_client.get("/health")
        assert response.status_code == status.HTTP_200_OK
        assert engine.disposed is False

    assert engine.disposed is True
