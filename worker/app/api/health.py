"""Worker health endpoint."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..config.settings import Settings


class HealthResponse(BaseModel):
    """Health payload returned by ``GET /health``."""

    status: str
    service: str
    version: str
    environment: str
    uptime_seconds: float
    timestamp: datetime


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    started_at: float = request.app.state.started_at
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version=settings.version,
        environment=settings.environment,
        uptime_seconds=max(0.0, round(time.monotonic() - started_at, 3)),
        timestamp=datetime.now(timezone.utc),
    )
