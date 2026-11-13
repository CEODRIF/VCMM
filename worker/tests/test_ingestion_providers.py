"""TASK 011 ingestion integration tests (spec sections 8, 16).

The REAL IngestionProcessor + REAL MaterializationService + REAL ffprobe
probe drive provider jobs end-to-end; only the Supabase-facing queue and
storage are fakes (no database, no network). Covers:

- direct-upload behavior UNCHANGED (regression, incl. progress sequence);
- the disabled master switch and unknown source types;
- metadata-permitted-but-materialization-denied → permanent, clear error;
- unconfigured provider → honest permanent error;
- status → typed queue category mapping (deleted, rate-limited);
- a fully AUTHORIZED fake provider end-to-end (materialize → store →
  pin → download → probe → complete);
- idempotent re-claim (already-pinned source skips the transfer);
- lease loss mid-transfer → abandon, NO failure write;
- retryable vs permanent provider failures;
- corrupt materialized bytes → the same media validation as uploads;
- no secret-shaped text can reach the job error record.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from app.config.settings import Settings
from app.processors.ingestion import IngestionProcessor
from app.providers.base import STATUS_AVAILABLE, ProviderCheck, StreamProvider
from app.providers.errors import (
    SourceCancelledError,
    TransferInterruptedError,
)
from app.providers.materialization import MaterializationService
from app.providers.models import NormalizedSource
from app.providers.twitch import TwitchConfig, TwitchProvider
from app.providers.youtube import YouTubeProvider
from app.queue.models import (
    ERROR_MATERIALIZATION_UNSUPPORTED,
    ERROR_PROVIDER_INVALID_URL,
    ERROR_PROVIDER_RATE_LIMITED,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_SOURCE_REJECTED,
    ERROR_SOURCE_UPLOAD,
    ERROR_TRANSFER_INTERRUPTED,
    ERROR_UNKNOWN,
)
from app.storage.supabase_storage import StorageDownloadError, StorageUploadError

JOB_ID = "11111111-1111-4111-8111-111111111111"
WORKER = "test-worker"
PUBLIC = "93.184.216.34"


def run(coro):
    return asyncio.run(coro)


def _resolver(*answers):
    return lambda hostname: answers


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# -- fakes (Supabase-facing only) ----------------------------------------------

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
        self.completions.append({"job_id": job_id, "worker_id": worker_id,
                                 "metadata": metadata})
        return True

    async def fail_job(self, job_id, worker_id, category, message, permanent) -> str:
        self.failures.append({"job_id": job_id, "category": category,
                              "message": message, "permanent": permanent})
        return "failed"

    async def set_job_source(self, job_id, worker_id, path) -> bool:
        if self.pin_ok:
            self.pins.append(path)
        return self.pin_ok


class FakeStorage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.downloads: list[str] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.fail_upload: bool = False

    async def download_to(self, object_path: str, destination: Path) -> int:
        self.downloads.append(object_path)
        data = self.objects.get(object_path)
        if data is None:
            raise StorageDownloadError("object missing")
        destination.write_bytes(data)
        return len(data)

    async def upload_source(self, job_id: str, source_file: Path,
                            content_type: str = "video/mp4") -> str:
        if self.fail_upload:
            raise StorageUploadError("simulated storage failure")
        path = f"sources/{job_id}/original/{uuid.uuid4()}.mp4"
        data = Path(source_file).read_bytes()
        self.uploads.append((path, data))
        self.objects[path] = data
        return path


def make_settings(tmp_path: Path, **overrides) -> Settings:
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "storage_root": tmp_path,
        "source_providers_enabled": True,
        "job_lease_seconds": 300,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def make_processor(settings: Settings, queue: FakeQueue, storage: FakeStorage,
                   providers: tuple[StreamProvider, ...],
                   ffprobe: str) -> IngestionProcessor:
    service = MaterializationService(storage, queue,  # type: ignore[arg-type]
                                     max_source_bytes=settings.materialize_max_source_bytes,
                                     chunk_bytes=settings.materialize_chunk_bytes,
                                     max_attempts=settings.provider_max_retries + 1,
                                     backoff_seconds=settings.provider_retry_backoff_seconds,
                                     max_redirects=settings.materialize_max_redirects)
    return IngestionProcessor(settings, queue, storage,  # type: ignore[arg-type]
                              providers=providers, materialization_service=service)


def job(source_type: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": JOB_ID, "_worker_id": WORKER,
                            "source_type": source_type, "source_path": None}
    base.update(extra)
    return base


# -- an AUTHORIZED fake provider (the gap the real adapters honestly leave) ----

class AuthorizedFakeProvider:
    """Stands in for a FUTURE licensed ingestion service: it claims to
    have an authorized media path and streams one (deterministic bytes).
    It exercises the full provider-independent materialization pipeline."""

    name = "youtube"
    supports_validation = True
    supports_materialization = True

    def __init__(self, video_bytes: bytes, *, materialize_exc: Exception | None = None,
                 validate_status: str = STATUS_AVAILABLE,
                 validate_reason: str | None = None) -> None:
        self._video = video_bytes
        self._materialize_exc = materialize_exc
        self._validate_status = validate_status
        self._validate_reason = validate_reason
        self.materialize_calls = 0

    def can_handle(self, url: str) -> bool:
        return url.startswith("https://youtu.be/")

    async def validate(self, url: str) -> ProviderCheck:
        return ProviderCheck(
            self._validate_status, self.name,
            title="Fake Authorized VOD",
            reason=self._validate_reason,
            metadata=NormalizedSource(
                provider=self.name,
                canonical_url="https://www.youtube.com/watch?v=abc123XYZ",
                provider_id="abc123XYZ", title="Fake Authorized VOD",
                state="completed" if self._validate_status == STATUS_AVAILABLE else "unknown",
            ),
        )

    async def materialize(self, url: str, destination_dir, *,
                          progress=None, should_cancel=None, **_: Any) -> Path:
        self.materialize_calls += 1
        if self._materialize_exc is not None:
            raise self._materialize_exc
        dest = Path(destination_dir) / "source.bin"
        dest.parent.mkdir(parents=True, exist_ok=True)
        chunk = 64 * 1024
        written = 0
        with dest.open("wb") as handle:
            while written < len(self._video):
                if should_cancel is not None and should_cancel():
                    break
                piece = self._video[written:written + chunk]
                handle.write(piece)
                written += len(piece)
                if progress is not None:
                    progress(written, len(self._video))
                await asyncio.sleep(0)  # let throttled progress writes run
        if written < len(self._video):
            dest.unlink(missing_ok=True)
            raise SourceCancelledError("cancelled")
        return dest


# =============================================================================
# Direct upload regression (behavior must be UNCHANGED)
# =============================================================================

def test_upload_job_behavior_unchanged(tmp_path, ffprobe_bin, synthetic_videos):
    video = synthetic_videos["valid"].read_bytes()
    object_path = f"sources/{JOB_ID}/original/33333333-3333-4333-8333-333333333333.mp4"
    queue, storage = FakeQueue(), FakeStorage({object_path: video})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    proc = make_processor(settings, queue, storage, (), ffprobe_bin)

    run(proc.process_job(job("upload", source_path=object_path)))

    assert queue.failures == []
    assert len(queue.completions) == 1
    meta = queue.completions[0]["metadata"]
    assert 1.5 <= meta["duration_seconds"] <= 2.5
    assert meta["width"] == 320 and meta["height"] == 240
    # exact Phase 3 progress sequence
    assert queue.progress == [10, 40, 80]
    assert storage.downloads == [object_path]
    # temp cleaned up
    assert not (settings.tmp_dir / JOB_ID).exists()


def test_upload_job_missing_object_fails_unchanged(tmp_path):
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path="ffprobe")
    proc = make_processor(settings, queue, storage, (), "ffprobe")
    run(proc.process_job(job("upload")))  # source_path=None
    assert queue.failures == [{
        "job_id": JOB_ID, "category": ERROR_UNKNOWN,
        "message": "job has no source object", "permanent": True,
    }]


# =============================================================================
# Gating: disabled master switch / unknown type / unconfigured provider
# =============================================================================

def test_disabled_master_switch_fails_permanently(tmp_path):
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path="ffprobe",
                             source_providers_enabled=False)
    yt = YouTubeProvider(http_client=_client(
        lambda r: httpx.Response(200, json={"title": "t", "type": "video"})),
        resolver=_resolver(PUBLIC))
    proc = make_processor(settings, queue, storage, (yt,), "ffprobe")

    run(proc.process_job(job("youtube", source_url="https://youtu.be/abc123XYZ")))

    assert len(queue.failures) == 1
    failure = queue.failures[0]
    assert failure["category"] == ERROR_PROVIDER_UNAVAILABLE
    assert failure["permanent"] is True
    assert "disabled" in failure["message"]
    assert storage.downloads == [] and storage.uploads == []


def test_unknown_source_type_fails_unchanged(tmp_path):
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path="ffprobe")
    yt = YouTubeProvider(resolver=_resolver(PUBLIC))
    proc = make_processor(settings, queue, storage, (yt,), "ffprobe")

    run(proc.process_job(job("vimeo", source_url="https://vimeo.com/1")))

    assert queue.failures == [{
        "job_id": JOB_ID, "category": ERROR_UNKNOWN,
        "message": "unsupported source type", "permanent": True,
    }]


def test_twitch_unconfigured_fails_honestly(tmp_path):
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path="ffprobe")
    twitch = TwitchProvider()  # no config
    proc = make_processor(settings, queue, storage, (twitch,), "ffprobe")

    run(proc.process_job(job("twitch", source_url="https://twitch.tv/videos/1")))

    assert len(queue.failures) == 1
    failure = queue.failures[0]
    assert failure["category"] == ERROR_PROVIDER_UNAVAILABLE
    assert failure["permanent"] is True
    assert "not configured" in failure["message"]
    assert "upload" in failure["message"]


def test_provider_job_without_url_fails_permanently(tmp_path):
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path="ffprobe")
    yt = YouTubeProvider(resolver=_resolver(PUBLIC))
    proc = make_processor(settings, queue, storage, (yt,), "ffprobe")

    run(proc.process_job(job("youtube")))  # no source_url

    assert queue.failures[0]["category"] == ERROR_PROVIDER_INVALID_URL
    assert queue.failures[0]["permanent"] is True


# =============================================================================
# Status → typed queue category (the honesty gates)
# =============================================================================

def test_youtube_metadata_ok_materialization_unsupported(tmp_path):
    """THE spec-7 outcome: metadata permitted, no authorized download path
    → PERMANENT, clear, actionable error; nothing is downloaded."""
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path="ffprobe")
    yt = YouTubeProvider(http_client=_client(
        lambda r: httpx.Response(200, json={"title": "V", "type": "video"})),
        resolver=_resolver(PUBLIC))
    proc = make_processor(settings, queue, storage, (yt,), "ffprobe")

    run(proc.process_job(job("youtube", source_url="https://youtu.be/abc123XYZ")))

    assert len(queue.failures) == 1
    failure = queue.failures[0]
    assert failure["category"] == ERROR_MATERIALIZATION_UNSUPPORTED
    assert failure["permanent"] is True
    assert "metadata" in failure["message"].lower()
    assert "upload the video file directly" in failure["message"].lower()
    assert storage.downloads == [] and storage.uploads == []


def test_twitch_deleted_vod_fails_permanently(tmp_path):
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path="ffprobe")
    twitch = TwitchProvider(http_client=_client(
        lambda r: httpx.Response(200, json={"data": []})),
        config=TwitchConfig(client_id="cid"), resolver=_resolver(PUBLIC))
    proc = make_processor(settings, queue, storage, (twitch,), "ffprobe")

    run(proc.process_job(job("twitch", source_url="https://twitch.tv/videos/123")))

    failure = queue.failures[0]
    assert failure["category"] == ERROR_SOURCE_REJECTED
    assert failure["permanent"] is True
    assert "deleted or removed" in failure["message"]


def test_twitch_rate_limited_is_transient(tmp_path):
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path="ffprobe")
    twitch = TwitchProvider(http_client=_client(lambda r: httpx.Response(429)),
                            config=TwitchConfig(client_id="cid"),
                            resolver=_resolver(PUBLIC))
    proc = make_processor(settings, queue, storage, (twitch,), "ffprobe")

    run(proc.process_job(job("twitch", source_url="https://twitch.tv/videos/123")))

    failure = queue.failures[0]
    assert failure["category"] == ERROR_PROVIDER_RATE_LIMITED
    assert failure["permanent"] is False


def test_secret_shaped_reason_is_redacted_in_job_record(tmp_path, synthetic_videos):
    """A provider reason (even a careless one) must never reach the job
    error record with credential-shaped text — the typed-error layer
    redacts on the way in."""
    token = "sk_live_abcdef1234567890"
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path="ffprobe")
    provider = AuthorizedFakeProvider(
        synthetic_videos["valid"].read_bytes(),
        validate_status="unavailable",
        validate_reason=f"upstream said authorization: Bearer {token}",
    )
    proc = make_processor(settings, queue, storage, (provider,), "ffprobe")

    run(proc.process_job(job("youtube", source_url="https://youtu.be/abc123XYZ")))

    assert len(queue.failures) == 1
    failure = queue.failures[0]
    assert token not in failure["message"]
    assert "***" in failure["message"]


# =============================================================================
# Authorized provider: full end-to-end materialization pipeline
# =============================================================================

def test_authorized_provider_end_to_end(tmp_path, ffprobe_bin, synthetic_videos):
    video = synthetic_videos["valid"].read_bytes()
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = AuthorizedFakeProvider(video)
    proc = make_processor(settings, queue, storage, (provider,), ffprobe_bin)

    run(proc.process_job(job("youtube", source_url="https://youtu.be/abc123XYZ")))

    # materialized exactly once, stored exactly once, pinned exactly once
    assert provider.materialize_calls == 1
    assert len(storage.uploads) == 1
    assert len(queue.pins) == 1
    assert storage.uploads[0][1] == video  # the bytes survived the round trip
    # the shared tail verified the stored object (download + real ffprobe)
    assert len(storage.downloads) == 1
    assert storage.downloads[0] == queue.pins[0]
    assert len(queue.completions) == 1
    meta = queue.completions[0]["metadata"]
    assert 1.5 <= meta["duration_seconds"] <= 2.5
    assert queue.failures == []
    # progress is monotonic, starts at 10, stores at 70, validates at 85
    assert queue.progress[0] == 10
    assert 70 in queue.progress and 85 in queue.progress
    assert all(b >= a for a, b in zip(queue.progress, queue.progress[1:]))
    assert not (settings.tmp_dir / JOB_ID).exists()


def test_authorized_provider_idempotent_reclaim(tmp_path, ffprobe_bin, synthetic_videos):
    """A re-claimed job that already has a pinned source NEVER re-transfers:
    the object is re-downloaded and re-verified instead (duplicate
    detection + interrupted-transfer recovery)."""
    video = synthetic_videos["valid"].read_bytes()
    pinned = f"sources/{JOB_ID}/original/44444444-4444-4444-8444-444444444444.mp4"
    queue, storage = FakeQueue(), FakeStorage({pinned: video})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = AuthorizedFakeProvider(video)
    proc = make_processor(settings, queue, storage, (provider,), ffprobe_bin)

    run(proc.process_job(job("youtube", source_url="https://youtu.be/abc123XYZ",
                             source_path=pinned)))

    assert provider.materialize_calls == 0
    assert storage.uploads == []  # no duplicate object
    assert len(queue.completions) == 1
    assert queue.failures == []


def test_lease_lost_mid_transfer_abandons_without_failure(tmp_path, synthetic_videos):
    video = synthetic_videos["valid"].read_bytes()
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path="ffprobe")
    provider = AuthorizedFakeProvider(video, materialize_exc=SourceCancelledError("lease lost"))
    proc = make_processor(settings, queue, storage, (provider,), "ffprobe")

    run(proc.process_job(job("youtube", source_url="https://youtu.be/abc123XYZ")))

    assert queue.failures == []       # NO failure write with a lost lease
    assert queue.completions == []
    assert storage.uploads == []


def test_transfer_interrupted_is_transient(tmp_path, synthetic_videos):
    video = synthetic_videos["valid"].read_bytes()
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path="ffprobe")
    provider = AuthorizedFakeProvider(video,
                                      materialize_exc=TransferInterruptedError("network drop"))
    proc = make_processor(settings, queue, storage, (provider,), "ffprobe")

    run(proc.process_job(job("youtube", source_url="https://youtu.be/abc123XYZ")))

    failure = queue.failures[0]
    assert failure["category"] == ERROR_TRANSFER_INTERRUPTED
    assert failure["permanent"] is False
    assert queue.completions == []


def test_storage_upload_failure_is_transient(tmp_path, ffprobe_bin, synthetic_videos):
    video = synthetic_videos["valid"].read_bytes()
    queue = FakeQueue()
    storage = FakeStorage({})
    storage.fail_upload = True
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = AuthorizedFakeProvider(video)
    proc = make_processor(settings, queue, storage, (provider,), ffprobe_bin)

    run(proc.process_job(job("youtube", source_url="https://youtu.be/abc123XYZ")))

    failure = queue.failures[0]
    assert failure["category"] == ERROR_SOURCE_UPLOAD
    assert failure["permanent"] is False
    assert queue.completions == []


def test_pin_lost_after_upload_abandons(tmp_path, ffprobe_bin, synthetic_videos):
    """set_job_source guard fails (lease lost after the upload): the job
    must abandon WITHOUT a failure write (bounded orphan documented)."""
    video = synthetic_videos["valid"].read_bytes()
    queue, storage = FakeQueue(pin_ok=False), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = AuthorizedFakeProvider(video)
    proc = make_processor(settings, queue, storage, (provider,), ffprobe_bin)

    run(proc.process_job(job("youtube", source_url="https://youtu.be/abc123XYZ")))

    assert queue.failures == []
    assert queue.completions == []
    assert len(storage.uploads) == 1  # uploaded, never pinned


def test_corrupt_materialized_bytes_fail_media_validation(tmp_path, ffprobe_bin):
    """Materialized bytes go through the SAME probe/validate as uploads —
    a 'successful' transfer of non-video bytes is still rejected."""
    queue, storage = FakeQueue(), FakeStorage({})
    settings = make_settings(tmp_path, ffprobe_path=ffprobe_bin)
    provider = AuthorizedFakeProvider(b"this is not a video" * 32)
    proc = make_processor(settings, queue, storage, (provider,), ffprobe_bin)

    run(proc.process_job(job("youtube", source_url="https://youtu.be/abc123XYZ")))

    assert queue.completions == []
    assert len(queue.failures) == 1
    assert queue.failures[0]["category"] in ("invalid_file", "corrupt_media")
    assert queue.failures[0]["permanent"] is True
