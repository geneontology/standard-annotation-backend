from fastapi import status
from fastapi.testclient import TestClient


def test_health_returns_ok_status(client: TestClient) -> None:
    """The health endpoint returns a successful status and readiness payload."""
    response = client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}
