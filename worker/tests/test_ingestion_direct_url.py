"""TASK 014 — direct-URL ingestion integration (REAL provider, fake
Supabase-facing queue/storage).

The REAL DirectUrlProvider (SSRF guard, redirect pre-resolution, bounded
streaming, pre-storage FFprobe) is driven end-to-end through
IngestionProcessor.process_job — the same code path a queued job takes:

  validate → materialize (stream + probe) → store_source (upload +
  idempotent pin) → download → finalize probe → complete

FakeQueue/FakeStorage/make_settings/make_processor/job follow
test_ingestion_providers.py exactly, so behavior parity with the other
provider types is structural.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from app.config.settings import Settings
from app.providers.direct_url import DirectUrlConfig, DirectUrlProvider
from app.providers.materialization import MaterializationService
from app.providers.upload import UploadProvider
from app.processors.ingestion import IngestionProcessor
from app.queue.models import (
    ERROR_CORRUPT_MEDIA,
    ERROR_PROVIDER_INVALID_URL,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_SOURCE_REJECTED,
    ERROR_TRANSFER_INTERRUPTED,
)
from app.storage.supabase_storage import StorageDownloadError, StorageUploadError

JOB_ID = "33333333-3333-4333-8333-333333333333"
WORKER = "test-worker"
PUBLIC = "93.184.216.34"
FILE_URL = "https://cdn.example.com/streams/vod.mp4"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _resolver(*answers_by_host: tuple[str, tuple[str, ...]]):
    table = dict(answers_by_host)

    def resolve(hostname: str):
        return table.get((hostname or "").lower(), ("NXDOMAIN",))

    return resolve


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FakeQueue:
    def __init__(self, pin_ok: bool = True) -> None:
        self.pin_ok = pin_ok
        self.progress: list[int] = []
        self.completions: list[dict] = []
        self.failures: list[dict] = []
        self.pins: list[str] = []

    async def update_job_progress(self, job_id, worker_id, value) -> bool:
        self.progress.append(int(value))
        return True

    async def renew_job_lease(self, job_id, worker_id, seconds) -> bool:
        return True

    async def complete_job_ingestion(self, job_id, worker_id, metadata) -> bool:
        self.completions.append({"job_id": job_id, "metadata": metadata})
        return True

    async def fail_job(self, job_id, worker_id, category, message, permanent) -> str:
        self.failures.append({"category": category, "message": message,
                              "permanent": permanent})
        return "failed"

    async def set_job_source(self, job_id, worker_id, path) -> bool:
        if self.pin_ok:
            self.pins.append(path)
        return self.pin_ok


class FakeStorage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.downloads: list[str] = []
        self.uploads: list[tuple[str, bytes, str]] = []

    async def download_to(self, object_path: str, destination: Path) -> int:
        self.downloads.append(object_path)
        data = self.objects.get(object_path)
        if data is None:
            raise StorageDownloadError("object missing")
        destination.write_bytes(data)
        return len(data)

    async def upload_source(self, job_id: str, source_file: Path,
                            content_type: str = "video/mp4") -> str:
        path = f"sources/{job_id}/original/source.mp4"
        self.uploads.append((path, source_file.read_bytes(), content_type))
        self.objects[path] = source_file.read_bytes()
        return path


def make_settings(tmp_path: Path, **overrides) -> Settings:
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "storage_root": tmp_path,
        "source_providers_enabled": True,
        "job_lease_seconds": 300,
        "ffprobe_path": "ffprobe",
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def make_processor(settings: Settings, queue: FakeQueue, storage: FakeStorage,
                   provider: DirectUrlProvider, ffprobe: str) -> IngestionProcessor:
    service = MaterializationService(storage, queue,  # type: ignore[arg-type]
                                     max_source_bytes=settings.materialize_max_source_bytes,
                                     chunk_bytes=settings.materialize_chunk_bytes,
                                     max_attempts=settings.provider_max_retries + 1,
                                     backoff_seconds=settings.provider_retry_backoff_seconds,
                                     max_redirects=settings.materialize_max_redirects)
    return IngestionProcessor(settings, queue, storage,  # type: ignore[arg-type]
                              providers=(UploadProvider(ffprobe), provider),
                              materialization_service=service)


def job(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": JOB_ID, "_worker_id": WORKER,
                            "source_type": "direct_url", "source_path": None,
                            "source_url": FILE_URL}
    base.update(extra)
    return base


def video_handler(video: bytes, state: dict, status: int = 200) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        state["hits"] = state.get("hits", 0) + 1
        if request.method == "HEAD":
            return httpx.Response(status, headers={"content-length": str(len(video))})
        return httpx.Response(status, content=video,
                              headers={"content-type": "video/mp4"})

    return handler


def _direct_provider(ffprobe_bin: str, handler: Any,
                     state: dict, tmp_path: Path,
                     **config_over: Any) -> DirectUrlProvider:
    base: dict[str, Any] = dict(
        max_source_bytes=10 * 1024 * 1024,
        chunk_bytes=64 * 1024,
        transfer_deadline_seconds=None,
    )
    base.update(config_over)
    config = DirectUrlConfig(**base)
    return DirectUrlProvider(
        ffprobe_bin, config=config,
        resolver=_resolver(("cdn.example.com", (PUBLIC,))),
        http_client=_client(handler),
    )


# =============================================================================
# Successful import → worker dispatch (real provider, full pipeline)
# =============================================================================

def test_direct_url_import_completes_and_dispatches(
        tmp_path, ffprobe_bin, synthetic_videos):
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = _direct_provider(ffprobe_bin, video_handler(video, state), state, tmp_path)
    proc = make_processor(settings, queue, storage, provider, ffprobe_bin)

    run(proc.process_job(job()))

    assert queue.failures == []
    assert len(queue.completions) == 1
    completion = queue.completions[0]
    assert completion["job_id"] == JOB_ID
    # The stored object holds the exact imported bytes.
    assert len(storage.uploads) == 1
    assert storage.uploads[0][1] == video
    assert storage.uploads[0][2] == "video/mp4"
    # Idempotent pin recorded; finalize downloaded the pinned object.
    assert len(queue.pins) == 1
    assert len(storage.downloads) == 1
    # Progress reported the real stages (claim → transfer → stored → probed).
    # The ingestion tail ends at 85; the analysis stage reports 100 later.
    assert 10 in queue.progress and 70 in queue.progress and 85 in queue.progress
    assert queue.progress == sorted(queue.progress)
    assert queue.progress[-1] == 85
    assert state["hits"] >= 2  # probe (HEAD) + stream (GET)


def test_direct_url_reclaim_is_idempotent(tmp_path, ffprobe_bin, synthetic_videos):
    """A crash between pin and complete: re-claim must NOT re-download the
    source from the URL — the pinned object is authoritative."""
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    pinned = f"sources/{JOB_ID}/original/source.mp4"
    queue, storage = FakeQueue(), FakeStorage({pinned: video})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = _direct_provider(ffprobe_bin, video_handler(video, state), state, tmp_path)
    proc = make_processor(settings, queue, storage, provider, ffprobe_bin)

    run(proc.process_job(job(source_path=pinned)))

    assert queue.failures == []
    assert len(queue.completions) == 1
    # The shared branch always re-validates availability (one HEAD probe);
    # it must NEVER re-download — the pinned object is authoritative.
    assert state["hits"] == 1
    assert storage.uploads == []         # no re-upload
    assert storage.downloads == [pinned]


def test_direct_url_webm_keeps_its_content_type(tmp_path, ffprobe_bin, synthetic_videos):
    """Per-extension storage content type (the worker chose the suffix)."""
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    url = "https://cdn.example.com/streams/clip.webm"

    def handler(request: httpx.Request) -> httpx.Response:
        state["hits"] = state.get("hits", 0) + 1
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(video))})
        return httpx.Response(200, content=video,
                              headers={"content-type": "video/webm"})

    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = _direct_provider(ffprobe_bin, handler, state, tmp_path)
    proc = make_processor(settings, queue, storage, provider, ffprobe_bin)

    run(proc.process_job(job(source_url=url)))

    assert queue.failures == []
    assert storage.uploads and storage.uploads[0][2] == "video/webm"


# =============================================================================
# Failure paths — accurate, categorized, no partial state
# =============================================================================

def test_direct_url_disabled_provider_fails_permanently(tmp_path, ffprobe_bin):
    """The operator switch OFF → the job fails with the honest, permanent
    'provider unavailable' error — never a silent download."""
    state: dict = {}
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = _direct_provider(ffprobe_bin, video_handler(b"", state), state,
                                tmp_path, enabled=False)
    proc = make_processor(settings, queue, storage, provider, ffprobe_bin)

    run(proc.process_job(job()))

    assert queue.completions == []
    assert len(queue.failures) == 1
    failure = queue.failures[0]
    assert failure["category"] == ERROR_PROVIDER_UNAVAILABLE
    assert failure["permanent"] is True
    assert "disabled" in failure["message"]
    assert storage.uploads == [] and state.get("hits", 0) == 0


def test_direct_url_ssrf_destination_fails_permanently(tmp_path, ffprobe_bin):
    """A pasted URL targeting the cloud metadata service → refused before
    any network traffic; permanent (the input cannot change on retry)."""
    state: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must NOT be reached")

    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = DirectUrlProvider(
        ffprobe_bin,
        resolver=_resolver(("169.254.169.254", ("169.254.169.254",))),
        http_client=_client(handler),
    )
    proc = make_processor(settings, queue, storage, provider, ffprobe_bin)

    run(proc.process_job(job(source_url="https://169.254.169.254/v.mp4")))

    assert queue.completions == []
    assert len(queue.failures) == 1
    failure = queue.failures[0]
    assert failure["category"] == ERROR_PROVIDER_INVALID_URL
    assert failure["permanent"] is True
    # The guard message is safe: no raw IP, no URL echoed.
    assert "169.254.169.254" not in failure["message"]
    assert "https://" not in failure["message"]
    assert storage.uploads == []


def test_direct_url_missing_file_is_source_rejected(tmp_path, ffprobe_bin):
    state: dict = {}
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = _direct_provider(ffprobe_bin, video_handler(b"", state, status=404),
                                state, tmp_path)
    proc = make_processor(settings, queue, storage, provider, ffprobe_bin)

    run(proc.process_job(job()))

    assert len(queue.failures) == 1
    failure = queue.failures[0]
    assert failure["category"] == ERROR_SOURCE_REJECTED
    assert failure["permanent"] is True
    assert storage.uploads == []


def test_direct_url_corrupt_bytes_fail_media_validation(tmp_path, ffprobe_bin):
    """A 200 response of NON-VIDEO bytes: the pre-storage FFprobe rejects
    it — nothing reaches the bucket, and the error says exactly that."""
    state: dict = {}
    page = b"<!doctype html><html><body>not a video</body></html>"
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = _direct_provider(ffprobe_bin, video_handler(page, state),
                                state, tmp_path)
    proc = make_processor(settings, queue, storage, provider, ffprobe_bin)

    run(proc.process_job(job()))

    assert queue.completions == []
    assert len(queue.failures) == 1
    failure = queue.failures[0]
    assert failure["category"] == ERROR_CORRUPT_MEDIA
    assert failure["permanent"] is True
    assert storage.uploads == []


def test_direct_url_oversized_source_fails_without_storage(tmp_path, ffprobe_bin,
                                                           synthetic_videos):
    """Size cap exceeded mid-transfer: retryable transfer error, partial
    bytes removed by the provider, NOTHING uploaded or pinned."""
    video = synthetic_videos["valid"].read_bytes()
    state: dict = {}
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = _direct_provider(ffprobe_bin, video_handler(video, state), state,
                                tmp_path, max_source_bytes=1024)
    proc = make_processor(settings, queue, storage, provider, ffprobe_bin)

    run(proc.process_job(job()))

    assert queue.completions == []
    assert len(queue.failures) == 1
    failure = queue.failures[0]
    assert failure["category"] == ERROR_TRANSFER_INTERRUPTED
    assert failure["permanent"] is False
    assert storage.uploads == [] and queue.pins == []
