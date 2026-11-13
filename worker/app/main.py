"""ClipForge AI worker — application entrypoint.

Phase 3: serves the health/heartbeat API and runs the ingestion worker loop
(claim → download → FFprobe validation → ready) alongside it via FastAPI
lifespan. Video processing runs ONLY in this worker, never on Vercel.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from .api.health import router as health_router
from .config.logging import configure_logging
from .config.settings import Settings, get_settings


def build_worker(settings: Settings):
    """Constructs the worker (or None when Supabase is not configured).

    Phase 4: the worker also builds the analysis path (transcription
    provider factory + clip store + AnalysisProcessor). With no whisper
    model configured the factory returns the honest "unavailable"
    provider — analysis jobs then fail with a clear permanent error
    instead of fabricating results.
    """
    if not (settings.worker_enabled and settings.supabase_configured):
        return None
    from supabase import create_async_client

    from .processors.analysis import AnalysisProcessor
    from .queue.supabase_queue import SupabaseQueue
    from .storage.clip_store import SupabaseClipStore
    from .storage.supabase_storage import SupabaseStorage
    from .transcription.factory import build_transcription_provider
    from .worker import IngestionWorker

    client = create_async_client(settings.supabase_url, settings.supabase_service_role_key)
    queue = SupabaseQueue(client)
    storage = SupabaseStorage(client, settings.max_upload_size_bytes)
    transcriber = build_transcription_provider(settings)
    clip_store = SupabaseClipStore(client)
    analysis = AnalysisProcessor(settings, queue, storage, transcriber, clip_store)
    return IngestionWorker(settings, queue, storage, analysis=analysis)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Application factory (explicit settings allow isolated tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = None
        try:
            worker = build_worker(settings)
        except Exception as exc:  # noqa: BLE001 — never take down the API
            import logging

            logging.getLogger("clipforge").error("worker init failed: %s", exc.__class__.__name__)
            worker = None
        app.state.worker = worker
        if worker is not None:
            await worker.start()
        else:
            import logging

            logging.getLogger("clipforge").info(
                "ingestion worker not started (disabled or Supabase not configured)"
            )
        yield
        if worker is not None:
            await worker.stop()

    app = FastAPI(
        title="ClipForge AI Worker",
        version=settings.version,
        description="Video processing worker for ClipForge AI (Phase 3: ingestion queue + validation; Phase 4: transcription + clip analysis).",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.started_at = time.monotonic()
    app.include_router(health_router)
    return app


app = create_app()
