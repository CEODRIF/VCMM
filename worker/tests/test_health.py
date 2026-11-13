"""Tests for the worker health endpoint."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.main import create_app


def make_client() -> TestClient:
    settings = Settings(environment="development", log_level="WARNING")
    return TestClient(create_app(settings))


def test_health_returns_ok():
    client = make_client()
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "clipforge-worker"
    assert body["version"] == "0.1.0"
    assert body["environment"] == "development"
    assert body["uptime_seconds"] >= 0
    # datetime is serialized as an ISO-8601 UTC string ("...Z" or "+00:00")
    parsed = datetime.fromisoformat(body["timestamp"])
    assert parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc).utcoffset().total_seconds() == 0


def test_health_reports_worker_heartbeat_when_present():
    client = make_client()

    class FakeWorker:
        @property
        def heartbeat(self):
            return {"worker_id": "w1:123:abc", "active_jobs": 2, "uptime_seconds": 12.5}

    client.app.state.worker = FakeWorker()
    body = client.get("/health").json()
    assert body["worker"] == {"worker_id": "w1:123:abc", "active_jobs": 2, "uptime_seconds": 12.5}


def test_health_reports_worker_none_when_not_configured():
    # Without Supabase credentials the ingestion loop is not started — the
    # field is present but null (API-only mode).
    client = make_client()
    client.app.state.worker = None
    body = client.get("/health").json()
    assert body["worker"] is None


def test_unknown_route_returns_404():
    client = make_client()
    assert client.get("/definitely-not-a-route").status_code == 404
