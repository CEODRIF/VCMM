"""Worker health endpoints (Phase 3 liveness, TASK 012 readiness + metrics).

- ``GET /health``  — LIVENESS: the process is up (Docker HEALTHCHECK target).
- ``GET /ready``   — READINESS (TASK 012, spec section 7): real, bounded
  probes of the dependencies the worker needs to do work. NEVER reports
  ready merely because the process started: database + storage must answer
  a live probe. Optional dependencies (transcription, facecam, game vision,
  provider configuration) are REPORTED but must not fail readiness while
  disabled — a missing optional dependency degrades its own feature only.
- ``GET /metrics`` — operational metrics in Prometheus text format
  (TASK 012, spec section 6; bounded cardinality, no paid services).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..config.settings import Settings
from ..observability import metrics as m

router = APIRouter(tags=["health"])


class WorkerHeartbeat(BaseModel):
    worker_id: str
    active_jobs: int
    uptime_seconds: float


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str
    uptime_seconds: float
    timestamp: datetime
    #: Present when the ingestion loop is configured; ``worker: None`` means
    #: the API is running without the ingestion worker (not configured).
    worker: WorkerHeartbeat | None = None


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    started_at: float = request.app.state.started_at
    worker = getattr(request.app.state, "worker", None)
    heartbeat = worker.heartbeat if worker is not None else None
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version=settings.version,
        environment=settings.environment,
        uptime_seconds=max(0.0, round(time.monotonic() - started_at, 3)),
        timestamp=datetime.now(timezone.utc),
        worker=heartbeat,
    )


@router.get("/ready")
async def ready(request: Request) -> PlainTextResponse:
    """Readiness: 200 only when the dependencies the worker needs to claim
    and process jobs are REACHABLE right now (worker mode), or when the
    deployment is intentionally API-only (no worker configured)."""
    import json

    worker = getattr(request.app.state, "worker", None)
    if worker is None:
        body = {
            "status": "ready",
            "worker": None,
            "components": {
                "database": "disabled",
                "storage": "disabled",
                "queue": "disabled",
                "optional": {},
                "providers": {},
            },
        }
        return PlainTextResponse(json.dumps(body, indent=2), media_type="application/json")

    components = await worker.probe_dependencies()
    operational = (
        components["database"] == "ok"
        and components["storage"] == "ok"
        and components["optional"].get("ffmpeg") in ("enabled",)
    )
    body = {
        "status": "ready" if operational else "degraded",
        "worker": worker.heartbeat,
        "components": components,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    status_code = 200 if operational else 503
    return PlainTextResponse(
        json.dumps(body, indent=2), media_type="application/json", status_code=status_code
    )


@router.get("/metrics")
def metrics(request: Request) -> PlainTextResponse:
    """Prometheus text exposition (bounded series; no secrets by
    construction — label domains are fixed enums)."""
    return PlainTextResponse(
        m.registry.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
