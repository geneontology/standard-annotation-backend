from fastapi.testclient import TestClient


def test_health_returns_ok_status(client: TestClient) -> None:
    """A missing or changed health response must fail this contract check."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
