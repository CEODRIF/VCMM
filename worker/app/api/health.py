"""Worker health endpoint (+ Phase 3 ingestion heartbeat)."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..config.settings import Settings


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


router = APIRouter(tags=["health"])


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
