"""TASK 011 materialization tests: the provider-independent transfer +
storage service (spec section 8) and the streaming source upload
(spec section 9: no whole-file memory, bounded, verified).

All deterministic: FakeStorage/FakeQueue (no Supabase), httpx.MockTransport
(no network), injected resolver (no DNS).
"""

from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path

import httpx
import pytest

from app.providers.errors import (
    SourceCancelledError,
    StorageFailedError,
    TransferInterruptedError,
)
from app.providers.http_guard import SafeHttpClient
from app.providers.materialization import MaterializationService
from app.storage.supabase_storage import (
    SOURCES_BUCKET,
    StorageUploadError,
    SupabaseStorage,
)

JOB_ID = "11111111-1111-4111-8111-111111111111"
WORKER = "test-worker"


def run(coro):
    return asyncio.run(coro)


def _resolver(*answers):
    return lambda hostname: answers


PUBLIC = "93.184.216.34"


# -- fakes -----------------------------------------------------------------------

class FakeStorage:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.uploads: list[tuple[str, Path, str]] = []

    async def upload_source(self, job_id: str, source_file: Path,
                            content_type: str = "video/mp4") -> str:
        if self.fail:
            raise StorageUploadError("simulated storage failure")
        self.uploads.append((job_id, Path(source_file), content_type))
        return f"sources/{job_id}/original/22222222-2222-4222-8222-222222222222.mp4"


class FakeQueue:
    def __init__(self, pin_ok: bool = True) -> None:
        self.pin_ok = pin_ok
        self.pins: list[tuple[str, str, str]] = []

    async def set_job_source(self, job_id: str, worker_id: str, path: str) -> bool:
        self.pins.append((job_id, worker_id, path))
        return self.pin_ok


def service(storage: FakeStorage, queue: FakeQueue, **kw) -> MaterializationService:
    return MaterializationService(storage, queue, max_source_bytes=1024 * 1024, **kw)


# =============================================================================
# store_source (idempotent, lease-guarded)
# =============================================================================

def test_store_source_happy_path(tmp_path):
    storage, queue = FakeStorage(), FakeQueue()
    svc = service(storage, queue)
    media = tmp_path / "source.bin"
    media.write_bytes(b"media-bytes")

    path = run(svc.store_source(JOB_ID, WORKER, media))

    assert path == f"sources/{JOB_ID}/original/22222222-2222-4222-8222-222222222222.mp4"
    assert storage.uploads == [(JOB_ID, media, "video/mp4")]
    assert queue.pins == [(JOB_ID, WORKER, path)]


def test_store_source_idempotent_when_already_pinned(tmp_path):
    """Duplicate source detection: a re-claimed job with a recorded
    source_path NEVER re-uploads (crash between pin and complete)."""
    storage, queue = FakeStorage(), FakeQueue()
    svc = service(storage, queue)
    existing = f"sources/{JOB_ID}/original/33333333-3333-4333-8333-333333333333.mp4"

    path = run(svc.store_source(JOB_ID, WORKER, tmp_path / "ignored.bin",
                                existing_source_path=existing))

    assert path == existing
    assert storage.uploads == []
    assert queue.pins == []


def test_store_source_lease_lost_abandons(tmp_path):
    storage, queue = FakeStorage(), FakeQueue(pin_ok=False)
    svc = service(storage, queue)
    media = tmp_path / "source.bin"
    media.write_bytes(b"x")

    with pytest.raises(SourceCancelledError):
        run(svc.store_source(JOB_ID, WORKER, media))
    # the object was uploaded but never pinned (bounded orphan, documented)
    assert len(storage.uploads) == 1


def test_store_source_storage_failure_is_typed_retryable(tmp_path):
    storage, queue = FakeStorage(fail=True), FakeQueue()
    svc = service(storage, queue)
    media = tmp_path / "source.bin"
    media.write_bytes(b"x")

    with pytest.raises(StorageFailedError) as exc:
        run(svc.store_source(JOB_ID, WORKER, media))
    assert exc.value.retryable is True
    assert queue.pins == []  # a failed upload is never pinned


# =============================================================================
# fetch_authorized_media (streaming, bounded, cancellable)
# =============================================================================

def _media_http(handler, **kw) -> SafeHttpClient:
    return SafeHttpClient(["cdn.licensed.example.com"],
                          transport=httpx.MockTransport(handler),
                          resolver=_resolver(PUBLIC), **kw)


def test_fetch_authorized_media_streams_to_file(tmp_path):
    body = b"V" * 1234
    progress = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body,
                              headers={"content-length": str(len(body))})

    storage, queue = FakeStorage(), FakeQueue()
    svc = service(storage, queue)
    http = _media_http(handler)
    dest = run(svc.fetch_authorized_media(
        http, "https://cdn.licensed.example.com/media.mp4", tmp_path,
        progress=lambda w, t: progress.append((w, t)),
    ))
    assert dest == tmp_path / "source.bin"
    assert dest.read_bytes() == body
    assert progress[-1] == (len(body), len(body))


def test_fetch_authorized_media_empty_file_is_an_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    svc = service(FakeStorage(), FakeQueue())
    http = _media_http(handler)
    with pytest.raises(TransferInterruptedError) as exc:
        run(svc.fetch_authorized_media(http, "https://cdn.licensed.example.com/e", tmp_path))
    assert "empty" in str(exc.value)
    assert not (tmp_path / "source.bin").exists()


def test_fetch_authorized_media_honors_cancel_probe(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"W" * 5000,
                              headers={"content-length": "5000"})

    svc = service(FakeStorage(), FakeQueue())
    http = _media_http(handler)
    with pytest.raises(SourceCancelledError):
        run(svc.fetch_authorized_media(
            http, "https://cdn.licensed.example.com/c", tmp_path,
            should_cancel=lambda: True,
        ))
    assert not (tmp_path / "source.bin").exists()


def test_fetch_authorized_media_ssrf_guard_applies(tmp_path):
    """The transfer client is guard-scoped: a URL to a NON-allowlisted
    host is refused before any request (even though it is 'authorized'
    upstream, the deployment decides which hosts are fetchable)."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be reached")

    from app.providers.errors import InvalidUrlError

    svc = service(FakeStorage(), FakeQueue())
    http = _media_http(handler)
    with pytest.raises(InvalidUrlError):  # the guard refuses before any request
        run(svc.fetch_authorized_media(http, "https://other.example.com/x", tmp_path))
    assert not (tmp_path / "source.bin").exists()


def test_build_http_uses_service_budgets():
    svc = service(FakeStorage(), FakeQueue(), max_redirects=2)
    http = svc.build_http(["cdn.licensed.example.com"], resolver=_resolver(PUBLIC))
    assert isinstance(http, SafeHttpClient)
    assert http._max_redirects == 2  # the service budget, not the default


# =============================================================================
# Real SupabaseStorage.upload_source — server-generated, streamed paths
# =============================================================================

class _FakeStorageAPI:
    """Mimics storage3: from_(bucket) → .upload(path, file, options)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def from_(self, bucket: str) -> "_FakeStorageAPI":
        self._bucket = bucket
        return self

    async def upload(self, path: str, file, options: dict | None) -> dict:
        # Prove the streaming contract: a file HANDLE (not whole bytes).
        head = file.read(16)
        self.calls.append({"bucket": self._bucket, "path": path,
                           "is_stream": isinstance(file, io.BufferedIOBase),
                           "is_whole_bytes": isinstance(file, (bytes, bytearray)),
                           "head": head, "options": options})
        return {"Key": path}


class _FakeSupabaseClient:
    def __init__(self) -> None:
        self.storage = _FakeStorageAPI()


def _real_storage(tmp_path: Path, max_bytes: int = 10_000_000) -> tuple[SupabaseStorage, _FakeSupabaseClient]:
    client = _FakeSupabaseClient()
    storage = SupabaseStorage(client, max_bytes)  # type: ignore[arg-type]
    return storage, client


def test_upload_source_path_shape_is_server_generated(tmp_path):
    storage, client = _real_storage(tmp_path)
    media = tmp_path / "v.bin"
    media.write_bytes(b"0123456789abcdef" * 4)

    path = run(storage.upload_source(JOB_ID, media, content_type="video/mp4"))

    pattern = (
        r"sources/" + JOB_ID +
        r"/original/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}\.mp4"
    )
    assert re.fullmatch(pattern, path)
    call = client.storage.calls[0]
    assert call["bucket"] == SOURCES_BUCKET
    # streamed handle over the file, NOT the whole file as bytes
    assert call["is_stream"] is True
    assert call["is_whole_bytes"] is False
    assert call["head"] == b"0123456789abcdef"
    assert call["options"]["upsert"] is False
    assert call["options"]["content-type"] == "video/mp4"


def test_upload_source_rejects_non_uuid_job_id(tmp_path):
    storage, _ = _real_storage(tmp_path)
    media = tmp_path / "v.bin"
    media.write_bytes(b"x")
    for bad in ("../../etc/passwd", "sources/x/original/a.mp4", "abc", ""):
        with pytest.raises(StorageUploadError):
            run(storage.upload_source(bad, media))


def test_upload_source_rejects_missing_empty_oversized(tmp_path):
    storage, _ = _real_storage(tmp_path)
    with pytest.raises(StorageUploadError):
        run(storage.upload_source(JOB_ID, tmp_path / "missing.bin"))
    empty = tmp_path / "e.bin"
    empty.write_bytes(b"")
    with pytest.raises(StorageUploadError):
        run(storage.upload_source(JOB_ID, empty))
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 1024)
    small_cap_storage, _ = _real_storage(tmp_path, max_bytes=10)
    with pytest.raises(StorageUploadError):
        run(small_cap_storage.upload_source(JOB_ID, big))


def test_upload_source_transport_failure_is_typed(tmp_path):
    class BoomAPI(_FakeStorageAPI):
        async def upload(self, path, file, options):
            raise RuntimeError("network down")

    client = _FakeSupabaseClient()
    client.storage = BoomAPI()
    storage = SupabaseStorage(client, 10_000_000)  # type: ignore[arg-type]
    media = tmp_path / "v.bin"
    media.write_bytes(b"data")
    with pytest.raises(StorageUploadError) as exc:
        run(storage.upload_source(JOB_ID, media))
    # the raw transport error (which may carry request context) is NOT leaked
    assert "network down" not in str(exc.value)
