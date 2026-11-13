"""ClipForge AI worker — application entrypoint.

Phase 1 exposes only the health endpoint. Queue consumption, video
processing and storage integration arrive in later phases
(see docs/ROADMAP.md).
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import FastAPI

from .api.health import router as health_router
from .config.logging import configure_logging
from .config.settings import Settings, get_settings


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Application factory (explicit settings allow isolated tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="ClipForge AI Worker",
        version=settings.version,
        description="Video processing worker for ClipForge AI (Phase 1: health endpoint only).",
    )
    app.state.settings = settings
    app.state.started_at = time.monotonic()
    app.include_router(health_router)
    return app


app = create_app()
