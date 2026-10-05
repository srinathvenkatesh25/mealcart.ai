from fastapi.testclient import TestClient

from app.main import create_app


def test_health_ok():
    with TestClient(create_app()) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_cors_allows_the_web_page():
    with TestClient(create_app()) as client:
        resp = client.options("/api/runs", headers={
            "Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"
