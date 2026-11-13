"""Clip upload tests (Phase 5): SupabaseStorage.upload_from with a mocked
transport. Every object path must match the renderer-generated shape
``clips/{job_id:uuid}/{clip_id:uuid}.mp4`` — no traversal, no arbitrary
extensions — and transfers are size-capped. Uploads are upserts (idempotent
re-renders).
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from app.storage.supabase_storage import CLIPS_BUCKET, SupabaseStorage, StorageUploadError

JOB = str(UUID("11111111-1111-4111-8111-111111111111"))
CLIP = str(UUID("22222222-2222-4222-8222-222222222222"))
VALID_PATH = f"clips/{JOB}/{CLIP}.mp4"


class FakeStorageBucket:
    def __init__(self, client, bucket):
        self._client = client
        self.bucket = bucket

    async def upload(self, path, file, file_options=None):
        self._client.uploads.append((self.bucket, path, file, file_options or {}))
        if self._client.upload_error:
            raise RuntimeError("transport exploded")
        return {"Key": f"{self.bucket}/{path}"}


class FakeClient:
    def __init__(self, upload_error=False):
        self.upload_error = upload_error
        self.uploads: list[tuple[str, str, bytes, dict]] = []
        self._buckets: dict[str, FakeStorageBucket] = {}

    @property
    def storage(self) -> "FakeClient":
        # The worker calls client.storage.from_(bucket).upload(...)
        return self

    def from_(self, bucket: str) -> FakeStorageBucket:
        return self._buckets.setdefault(bucket, FakeStorageBucket(self, bucket))


def run(coro):
    return asyncio.run(coro)


def make_storage(tmp_path, content: bytes = b"rendered-mp4-bytes",
                 max_clip_bytes: int | None = None, upload_error=False) -> tuple[SupabaseStorage, FakeClient, object]:
    client = FakeClient(upload_error=upload_error)
    storage = SupabaseStorage(client, max_download_bytes=1000, max_clip_bytes=max_clip_bytes)
    file = tmp_path / "clip.mp4"
    file.write_bytes(content)
    return storage, client, file


def test_upload_success_records_upsert_with_content_type(tmp_path):
    storage, client, file = make_storage(tmp_path)
    sent = run(storage.upload_from(VALID_PATH, file))
    assert sent == len(b"rendered-mp4-bytes")
    assert len(client.uploads) == 1
    bucket, path, content, options = client.uploads[0]
    assert bucket == CLIPS_BUCKET
    assert path == VALID_PATH
    assert content == b"rendered-mp4-bytes"
    assert options == {"content-type": "video/mp4", "upsert": True}


@pytest.mark.parametrize("bad_path", [
    f"clips/{JOB}/../../etc/passwd",          # traversal
    f"clips/{JOB}/{CLIP}",                    # missing .mp4
    f"clips/{JOB}/{CLIP}.mov",                # wrong extension
    f"clips/not-a-uuid/{CLIP}.mp4",           # job id not a uuid
    f"clips/{JOB}/not-a-uuid.mp4",            # clip id not a uuid
    f"clips/{JOB}/{CLIP}.mp4/extra",          # extra segment
    f"sources/{JOB}/original/video.mp4",      # wrong tree entirely
    "",                                       # empty
])
def test_invalid_object_paths_are_refused_before_transport(tmp_path, bad_path):
    storage, client, file = make_storage(tmp_path)
    with pytest.raises(StorageUploadError):
        run(storage.upload_from(bad_path, file))
    assert client.uploads == []  # nothing ever reached the transport


def test_missing_file_is_refused(tmp_path):
    storage, client, _ = make_storage(tmp_path)
    with pytest.raises(StorageUploadError):
        run(storage.upload_from(VALID_PATH, tmp_path / "nope.mp4"))
    assert client.uploads == []


def test_empty_file_is_refused(tmp_path):
    storage, client, file = make_storage(tmp_path, content=b"")
    with pytest.raises(StorageUploadError):
        run(storage.upload_from(VALID_PATH, file))
    assert client.uploads == []


def test_explicit_size_cap_is_enforced(tmp_path):
    storage, client, file = make_storage(tmp_path, content=b"x" * 100)
    with pytest.raises(StorageUploadError):
        run(storage.upload_from(VALID_PATH, file, max_bytes=50))
    assert client.uploads == []
    # Within the cap: allowed.
    assert run(storage.upload_from(VALID_PATH, file, max_bytes=100)) == 100
    assert len(client.uploads) == 1


def test_default_size_cap_comes_from_constructor(tmp_path):
    storage, client, file = make_storage(tmp_path, content=b"x" * 100, max_clip_bytes=50)
    with pytest.raises(StorageUploadError):
        run(storage.upload_from(VALID_PATH, file))
    assert client.uploads == []


def test_transport_error_is_wrapped_safely(tmp_path):
    storage, client, file = make_storage(tmp_path, upload_error=True)
    with pytest.raises(StorageUploadError) as exc:
        run(storage.upload_from(VALID_PATH, file))
    # Safe message: no transport internals.
    assert "transport" not in str(exc).lower()
    assert "exploded" not in str(exc)
