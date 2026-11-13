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


# -- TASK 012: readiness + metrics ------------------------------------------------------


class FakeProbeWorker:
    """Worker double with a canned probe_dependencies() payload."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.probe_calls = 0

    @property
    def heartbeat(self):
        return {"worker_id": "w:1:abc", "active_jobs": 0, "uptime_seconds": 1.0}

    async def probe_dependencies(self, timeout: float = 5.0, cache_ttl: float = 30.0) -> dict:
        self.probe_calls += 1
        return self._payload


def _ok_components(**optional_overrides) -> dict:
    optional = {
        "ffmpeg": "enabled",
        "transcription": "disabled",
        "facecam": "disabled",
        "game_vision": "disabled",
    }
    optional.update(optional_overrides)
    return {
        "database": "ok",
        "storage": "ok",
        "queue": "ok",
        "optional": optional,
        "providers": {"youtube": "keyless-oembed", "twitch": "unconfigured",
                      "master_switch": "on", "materialization": "unsupported"},
    }


def test_ready_reports_ready_when_worker_dependencies_are_up():
    client = make_client()
    client.app.state.worker = FakeProbeWorker(_ok_components())
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["components"]["database"] == "ok"
    assert body["components"]["storage"] == "ok"
    assert body["components"]["queue"] == "ok"
    assert body["worker"]["worker_id"] == "w:1:abc"


def test_ready_reports_503_when_database_is_down():
    client = make_client()
    components = _ok_components()
    components["database"] = "error"
    components["queue"] = "error"
    client.app.state.worker = FakeProbeWorker(components)
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"]["database"] == "error"


def test_ready_reports_503_when_storage_is_down():
    client = make_client()
    components = _ok_components()
    components["storage"] = "error"  # top-level component, like the DB test
    client.app.state.worker = FakeProbeWorker(components)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["components"]["storage"] == "error"


def test_ready_gates_on_ffmpeg_but_not_on_optional_features():
    """ffmpeg is core (degraded -> not ready); a DEGRADED facecam with core
    dependencies healthy must NOT fail readiness (spec section 7)."""
    client = make_client()
    # facecam degraded, core fine -> ready.
    ok = _ok_components(facecam="degraded")
    client.app.state.worker = FakeProbeWorker(ok)
    ready_resp = client.get("/ready")
    assert ready_resp.status_code == 200
    assert ready_resp.json()["status"] == "ready"
    # ffmpeg degraded, core fine -> NOT ready (all video work needs it).
    bad = _ok_components(ffmpeg="degraded")
    client.app.state.worker = FakeProbeWorker(bad)
    degraded_resp = client.get("/ready")
    assert degraded_resp.status_code == 503
    assert degraded_resp.json()["status"] == "degraded"


def test_ready_api_only_mode_reports_ready_with_disabled_components():
    client = make_client()
    client.app.state.worker = None
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["worker"] is None
    assert body["components"]["database"] == "disabled"
    assert body["components"]["storage"] == "disabled"
    assert body["components"]["queue"] == "disabled"


def test_metrics_exposes_prometheus_text_with_core_families():
    client = make_client()
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    for family in (
        "clipforge_jobs_claimed_total",
        "clipforge_jobs_completed_total",
        "clipforge_jobs_failed_total",
        "clipforge_worker_active_jobs",
        "clipforge_storage_failures_total",
    ):
        assert f"# TYPE {family} " in text
    # Metrics must never carry secrets.
    assert "token" not in text
    assert "service_role" not in text
    assert "password" not in text


def test_metrics_is_stable_and_reproducible():
    client = make_client()
    first = client.get("/metrics").text
    second = client.get("/metrics").text
    assert first == second  # rendering is deterministic
    assert first.endswith("\n")
