from fastapi.testclient import TestClient

from standard_annotation_backend.main import app


def test_health_returns_ok_status() -> None:
    """A missing or changed health response must fail this contract check."""
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
