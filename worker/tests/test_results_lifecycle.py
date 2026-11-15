"""TASK 015 P4 — results lifecycle tests (worker).

Three layers, distinguished on purpose:

1. FFmpeg INTEGRATION (real renderer + fake transport): the render
   processor persists the MEASURED output contract (width/height/fps/
   file size) together with 'ready' — read back from a genuine render.

2. Source retention (fake storage): 'keep' (default) never deletes the
   source; 'delete_on_complete' deletes it ONLY after the DB accepted
   completion; a failed deletion does NOT fail the finished job.

3. Lifecycle units (fake supabase client): SupabaseStorage.delete_object /
   object_exists verification semantics (never "success" without verified
   absence) and the OrphanCleanup sweep (bounded, resumable, owner-row
   checked, grace-period protected, canonical paths only).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.cleanup import OrphanCleanup, OrphanResult
from app.storage.supabase_storage import SupabaseStorage, StorageDownloadError
from test_render_processor import (
    FakeClipStore,
    FakeQueue,
    FakeStorage,
    JOB_ID,
    RecordingFFmpegRenderer,
    WORKER,
    clip,
    make_job,
    make_processor,
    make_settings,
)


def run(coro):
    return asyncio.run(coro)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 1) FFmpeg integration — measured metadata persisted with 'ready'
# ---------------------------------------------------------------------------

def test_render_processor_persists_measured_metadata(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin, render_concurrency=1)
    queue = FakeQueue()
    storage = FakeStorage(render_media["landscape"].read_bytes())
    a = _uuid()
    store = FakeClipStore([clip(a, 0.0, 3.0, order=0)])
    renderer = RecordingFFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    processor = make_processor(settings, queue, storage, store, renderer)
    job = make_job()
    job["output_fps"] = 30

    run(processor.process_job(job))

    assert queue.complete_calls == [(JOB_ID, WORKER)]
    # The metadata was written with the 'ready' transition — and it is the
    # REAL ffprobe measurement of a genuine 1080x1920 30fps render.
    assert len(store.mark_meta) == 1
    meta = store.mark_meta[0]
    assert meta["width"] == 1080
    assert meta["height"] == 1920
    assert meta["output_fps"] == pytest.approx(30.0, rel=0.05)
    assert meta["file_size_bytes"] > 0
    assert meta["file_size_bytes"] == storage.uploads[0][1]  # bytes actually stored


# ---------------------------------------------------------------------------
# 2) Source retention policy
# ---------------------------------------------------------------------------

def test_source_retention_keep_default_preserves_the_source(ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    settings = make_settings(tmp_path, ffmpeg_bin, ffprobe_bin, render_concurrency=1)
    queue = FakeQueue()
    storage = FakeStorage(render_media["landscape"].read_bytes())
    a = _uuid()
    store = FakeClipStore([clip(a, 0.0, 3.0, order=0)])
    renderer = RecordingFFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    processor = make_processor(settings, queue, storage, store, renderer)

    run(processor.process_job(make_job()))

    assert queue.complete_calls == [(JOB_ID, WORKER)]
    assert storage.deletes == []  # default 'keep': the source is untouched


def test_source_retention_delete_on_complete_deletes_after_all_clips_ready(
        ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    settings = make_settings(
        tmp_path, ffmpeg_bin, ffprobe_bin, render_concurrency=1,
        source_retention="delete_on_complete",
    )
    queue = FakeQueue()
    storage = FakeStorage(render_media["landscape"].read_bytes())
    a, b = _uuid(), _uuid()
    store = FakeClipStore([clip(a, 0.0, 3.0, order=0), clip(b, 3.0, 6.0, order=1)])
    renderer = RecordingFFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    processor = make_processor(settings, queue, storage, store, renderer)

    run(processor.process_job(make_job()))

    # The job completed (every clip ready+stored) AND the source was deleted
    # — the generated clips remain (separate bucket, uploaded first).
    assert queue.complete_calls == [(JOB_ID, WORKER)]
    assert storage.deletes == [f"sources/{JOB_ID}/original/video.mp4"]
    assert len(storage.uploads) == 2  # both clips safely persisted


def test_source_deletion_failure_does_not_fail_a_finished_job(
        ffmpeg_bin, ffprobe_bin, render_media, tmp_path):
    """Retention is best-effort: the artifacts are complete, so a failed
    source deletion degrades to a warning (Phase 6 purge is the backstop)."""
    settings = make_settings(
        tmp_path, ffmpeg_bin, ffprobe_bin, render_concurrency=1,
        source_retention="delete_on_complete",
    )
    queue = FakeQueue()
    storage = FakeStorage(render_media["landscape"].read_bytes(), delete_error=True)
    a = _uuid()
    store = FakeClipStore([clip(a, 0.0, 3.0, order=0)])
    renderer = RecordingFFmpegRenderer(ffmpeg_bin, ffprobe_bin, timeout_seconds=120)
    processor = make_processor(settings, queue, storage, store, renderer)

    run(processor.process_job(make_job()))

    assert queue.complete_calls == [(JOB_ID, WORKER)]  # job still completes
    assert queue.fail_calls == []


# ---------------------------------------------------------------------------
# 3) Storage deletion semantics (fake supabase client)
# ---------------------------------------------------------------------------

class _FakeStorageApi:
    def __init__(self, present: set[str], fail_remove: bool = False):
        self.present = set(present)
        self.fail_remove = fail_remove
        self.removed: list[str] = []

    async def list(self, directory, options=None):
        # Emulate: listing a directory returns the entries living in it.
        entries = []
        for path in self.present:
            parent, _, leaf = path.rpartition("/")
            if parent == (directory or ""):
                entries.append({"name": leaf, "id": None})
        return entries

    async def remove(self, paths):
        if self.fail_remove:
            raise RuntimeError("storage unavailable")
        self.removed.extend(paths)
        for p in paths:
            self.present.discard(p)


class _FakeClient:
    def __init__(self, present: set[str], fail_remove: bool = False):
        self._api = _FakeStorageApi(present, fail_remove)
        self.storage = self

    def from_(self, bucket):
        return self._api


def test_delete_object_requires_verified_absence():
    obj = f"clips/{JOB_ID}/{_uuid()}.mp4"
    store = SupabaseStorage(_FakeClient({obj}), max_download_bytes=1024)
    # valid deletion: removed + verified absent -> True
    assert run(store.delete_object(obj, source=False)) is True
    # idempotent: already missing -> True
    assert run(store.delete_object(obj, source=False)) is True


def test_delete_object_never_succeeds_without_verification():
    # The object comes BACK after removal (simulated storage replication):
    # the absence check fails -> False, never "success".
    class Reappearing(_FakeStorageApi):
        async def remove(self, paths):
            for p in paths:
                self.present.add(p)  # it reappears — absence can't be verified

    obj = f"clips/{JOB_ID}/{_uuid()}.mp4"
    client = _FakeClient(set())
    client.from_ = lambda bucket: Reappearing({obj})
    store = SupabaseStorage(client, max_download_bytes=1024)
    assert run(store.delete_object(obj, source=False)) is False


def test_delete_object_rejects_non_canonical_paths():
    store = SupabaseStorage(_FakeClient(set()), max_download_bytes=1024)
    with pytest.raises(StorageDownloadError):
        run(store.delete_object("sources/other-bucket/file.mp4", source=True))
    with pytest.raises(Exception):
        run(store.delete_object("../escape.mp4", source=False))


# ---------------------------------------------------------------------------
# 4) Orphan cleanup (fake supabase client, deterministic)
# ---------------------------------------------------------------------------

class _OrphanFakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._filters: list[tuple[str, object]] = []

    def select(self, *cols):
        return self

    def eq(self, col, value):
        self._filters.append((col, value))
        return self

    def limit(self, n):
        return self

    async def execute(self):
        out = [r for r in self._rows if all(r.get(c) == v for c, v in self._filters)]
        return type("R", (), {"data": out})()


class _OrphanFakeClient:
    def __init__(self, trees: dict[tuple[str, str], list], tables: dict[str, list]):
        self._trees = trees
        self._tables = tables
        self.removed: list[tuple[str, str]] = []

    def table(self, name):
        return _OrphanFakeQuery(self._tables.get(name, []))

    @property
    def storage(self):
        outer = self

        class _S:
            def from_(self, bucket):
                return outer._StorageBucket(outer, bucket)

        return _S()

    class _StorageBucket:
        def __init__(self, owner, bucket):
            self._owner = owner
            self._bucket = bucket

        async def list(self, directory, options=None):
            return self._owner._trees.get((self._bucket, directory), [])

        async def remove(self, paths):
            for p in paths:
                self._owner.removed.append((self._bucket, p))
            return None


NOW = datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=2)).isoformat()
RECENT = (NOW - timedelta(minutes=5)).isoformat()


def _orphan_fixture():
    job_orphan, job_owner = _uuid(), _uuid()
    job_orphan_src, job_owner_src = _uuid(), _uuid()
    clip_orphan, clip_owner, clip_recent = _uuid(), _uuid(), _uuid()

    trees = {
        ("clipforge-clips", "clips"): [
            {"name": job_orphan, "id": None},
            {"name": job_owner, "id": None},
            {"name": "stray.mp4", "id": "not-a-dir"},
        ],
        ("clipforge-clips", f"clips/{job_orphan}"): [
            {"name": f"{clip_orphan}.mp4", "id": None, "created_at": OLD, "updated_at": OLD},
            {"name": "junk.txt", "id": None, "created_at": OLD},
        ],
        ("clipforge-clips", f"clips/{job_owner}"): [
            {"name": f"{clip_owner}.mp4", "id": None, "created_at": OLD, "updated_at": OLD},
            {"name": f"{clip_recent}.mp4", "id": None, "created_at": RECENT, "updated_at": RECENT},
        ],
        ("clipforge-sources", "sources"): [
            {"name": job_orphan_src, "id": None},
            {"name": job_owner_src, "id": None},
        ],
        ("clipforge-sources", f"sources/{job_orphan_src}/original"): [
            {"name": "video.mp4", "id": None, "created_at": OLD, "updated_at": OLD},
        ],
        ("clipforge-sources", f"sources/{job_owner_src}/original"): [
            {"name": "video.mp4", "id": None, "created_at": OLD, "updated_at": OLD},
        ],
    }
    tables = {
        "clips": [{"job_id": job_owner, "id": clip_owner}],  # clip_owner has an owner
        "jobs": [{"id": job_owner_src}],  # job_owner_src exists -> its source is safe
    }
    ids = {
        "job_orphan": job_orphan, "job_owner": job_owner,
        "clip_orphan": clip_orphan, "clip_owner": clip_owner,
        "clip_recent": clip_recent,
        "job_orphan_src": job_orphan_src, "job_owner_src": job_owner_src,
    }
    return _OrphanFakeClient(trees, tables), ids


def test_orphan_sweep_deletes_only_ownerless_canonical_objects():
    client, ids = _orphan_fixture()
    cleaner = OrphanCleanup(client, orphan_grace_minutes=60)
    results = run(cleaner.sweep(limit=10, now=NOW))
    by_bucket = {r.bucket: r for r in results}

    removed = set(client.removed)
    # The ownerless CLIP object (old enough) is deleted...
    assert ("clipforge-clips", f"clips/{ids['job_orphan']}/{ids['clip_orphan']}.mp4") in removed
    # ...but the clip whose row exists is NEVER touched...
    assert ("clipforge-clips", f"clips/{ids['job_owner']}/{ids['clip_owner']}.mp4") not in removed
    # ...nor is a fresh orphan (grace period protects in-flight downloads).
    assert ("clipforge-clips", f"clips/{ids['job_owner']}/{ids['clip_recent']}.mp4") not in removed
    # The orphaned SOURCE (job row gone) is deleted; the owned source is kept.
    assert ("clipforge-sources", f"sources/{ids['job_orphan_src']}/original/video.mp4") in removed
    assert ("clipforge-sources", f"sources/{ids['job_owner_src']}/original/video.mp4") not in removed
    # Nothing non-canonical is ever deleted.
    assert all(path.count("/") >= 2 and path.startswith(("clips/", "sources/")) for _, path in removed)

    clips_r = by_bucket["clipforge-clips"]
    assert isinstance(clips_r, OrphanResult)
    assert clips_r.ok and clips_r.deleted == 1
    assert clips_r.skipped_grace == 1     # the recent orphan
    assert clips_r.skipped_invalid == 2   # stray.mp4 at the root + junk.txt
    sources_r = by_bucket["clipforge-sources"]
    assert sources_r.ok and sources_r.deleted == 1


def test_orphan_sweep_is_bounded_and_resumable():
    client, _ids = _orphan_fixture()
    cleaner = OrphanCleanup(client, orphan_grace_minutes=60)
    first = run(cleaner.sweep(limit=1, now=NOW))  # only ONE job dir per bucket
    clips_first = next(r for r in first if r.bucket == "clipforge-clips")
    # With limit=1 only the FIRST job dir (the orphan one) was scanned.
    assert clips_first.scanned == 1
    assert clips_first.deleted == 1
    # A second run resumes: same deterministic outcome, no crash, and the
    # already-deleted object is simply handled again idempotently.
    second = run(cleaner.sweep(limit=10, now=NOW))
    clips_second = next(r for r in second if r.bucket == "clipforge-clips")
    assert clips_second.ok
    assert sum(r.deleted for r in second) <= sum(r.deleted for r in first) + 1


def test_orphan_sweep_assumes_presence_when_owner_checks_fail():
    """When the owner-row check itself errors, the object is treated as
    PRESENT (safe default) — the sweep never deletes on uncertainty."""
    client, _ids = _orphan_fixture()

    class BoomQuery:
        def __init__(self, *a, **k):
            pass

        def select(self, *a):
            return self

        def eq(self, *a):
            return self

        def limit(self, *a):
            return self

        async def execute(self):
            raise RuntimeError("db down")

    client.table = lambda name: BoomQuery()
    cleaner = OrphanCleanup(client, orphan_grace_minutes=60)
    results = run(cleaner.sweep(limit=10, now=NOW))
    assert all(r.deleted == 0 for r in results)  # nothing deleted under uncertainty
    assert client.removed == []


def test_orphan_sweep_grace_period_never_deletes_recent_objects():
    client, _ids = _orphan_fixture()
    cleaner = OrphanCleanup(client, orphan_grace_minutes=60)
    # 'now' is only 30 minutes after the OLD objects were created (inside
    # the 60-minute grace); the RECENT object is in the future from here.
    # Nothing with a timestamp may be deleted in this run.
    results = run(cleaner.sweep(limit=10, now=datetime(2026, 11, 28, 12, 30, tzinfo=timezone.utc)))
    assert all(r.deleted == 0 and r.skipped_grace >= 1 for r in results if r.ok)
