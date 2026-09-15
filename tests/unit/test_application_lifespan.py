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


def test_lifespan_builds_unit_of_work_factory_without_connecting_and_disposes_engine(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup prepares database access lazily and shutdown disposes the engine."""
    engine = _ConnectionlessEngine()
    session_factory = object()
    expected_unit_of_work_factory = object()
    monkeypatch.setattr(
        main_module,
        "create_database_engine",
        lambda _url: engine,
        raising=False,
    )
    monkeypatch.setattr(
        main_module,
        "create_session_factory",
        lambda _engine: session_factory,
        raising=False,
    )
    monkeypatch.setattr(
        main_module,
        "create_unit_of_work_factory",
        lambda _session_factory: expected_unit_of_work_factory,
        raising=False,
    )

    with TestClient(main_module.app) as local_client:
        response = local_client.get("/health")
        assert response.status_code == status.HTTP_200_OK
        assert main_module.app.state.unit_of_work_factory is (
            expected_unit_of_work_factory
        )
        assert engine.disposed is False

    assert engine.disposed is True
