from fastapi.testclient import TestClient

from app.version import __version__


def test_health_includes_version(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    # Envelope middleware wraps payloads.
    data = body.get("data", body)
    assert data["version"] == __version__
    assert __version__ == "0.4.0"
