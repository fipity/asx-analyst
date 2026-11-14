from fastapi.testclient import TestClient

from app.config import get_settings


def client():
    from app.main import app
    return TestClient(app)


def test_health_and_pages():
    with client() as c:
        assert c.get("/healthz").json() == {"ok": True}
        r = c.get("/status")
        assert r.status_code == 200 and "Data freshness" in r.text
        assert "not financial advice" in r.text
        assert "delayed 20+ minutes" in r.text
        assert c.get("/").status_code == 200
        assert c.get("/api/status").json()["universe"]["index"] == "asx200"


def test_password_gate(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    get_settings.cache_clear()
    with client() as c:
        r = c.get("/status", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert c.get("/api/status").status_code == 401
        assert c.get("/healthz").status_code == 200
        r = c.post("/login", data={"password": "wrong", "next": "/status"})
        assert "Wrong password" in r.text
        r = c.post("/login", data={"password": "s3cret", "next": "/status"}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/status").status_code == 200


def test_unknown_job_404():
    with client() as c:
        assert c.post("/api/jobs/nope/run").status_code == 404
