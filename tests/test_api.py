from __future__ import annotations

from fastapi.testclient import TestClient

from fis.api import create_app


def test_ping() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
