"""Phase 6 cleanup tests: retention policy, job-scoped deletion, idempotency,
partial-failure safety and the manual CLI — all against an in-memory fake of
the Supabase table + storage API (no network, no live project)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.cleanup import PurgeService, run_cleanup
from app.storage.supabase_storage import CLIPS_BUCKET, SOURCES_BUCKET

NOW = datetime(2026, 11, 13, 0, 0, 0, tzinfo=timezone.utc)
EXPIRED = "2026-11-01T00:00:00Z"
FUTURE_DEADLINE = "2026-12-01T00:00:00Z"
ALREADY_PURGED_AT = "2026-10-05T00:00:00Z"

JOB_A = str(UUID("11111111-1111-4111-8111-111111111111"))  # completed, expired, un-purged
JOB_B = str(UUID("22222222-2222-4222-8222-222222222222"))  # failed, deadline in the future
JOB_C = str(UUID("33333333-3333-4333-8333-333333333333"))  # completed, expired, already purged
JOB_D = str(UUID("44444444-4444-4444-8444-444444444444"))  # rendering (active), expired deadline
JOB_E = str(UUID("55555555-5555-4555-8555-555555555555"))  # completed, no deadline at all

CLIP_A1 = str(UUID("66666666-6666-4666-8666-666666666666"))
CLIP_A2 = str(UUID("77777777-7777-4777-8777-777777777777"))
CLIP_A3 = str(UUID("88888888-8888-4888-8888-888888888888"))
CLIP_A4 = str(UUID("99999999-9999-4999-8999-999999999999"))


def run(coro):
    return asyncio.run(coro)


# -- fake Supabase (tables + storage) -----------------------------------------------------


def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class FakeQuery:
    """Minimal chainable builder: select/update + eq/in_/lt/is_/order/limit."""

    def __init__(self, store: "FakeStore", table: str) -> None:
        self._store = store
        self._table = table
        self._filters: list[tuple[str, str, object]] = []
        self._mode = "select"
        self._values: dict = {}
        self._limit: int | None = None

    def select(self, _cols: str) -> "FakeQuery":
        self._mode = "select"
        return self

    def update(self, values: dict) -> "FakeQuery":
        self._mode = "update"
        self._values = values
        return self

    def eq(self, col: str, value: object) -> "FakeQuery":
        self._filters.append(("eq", col, value))
        return self

    def in_(self, col: str, values: list) -> "FakeQuery":
        self._filters.append(("in", col, values))
        return self

    def lt(self, col: str, value: object) -> "FakeQuery":
        self._filters.append(("lt", col, value))
        return self

    def is_(self, col: str, value: object) -> "FakeQuery":
        self._filters.append(("is", col, value))
        return self

    def order(self, _col: str) -> "FakeQuery":
        return self

    def limit(self, n: int) -> "FakeQuery":
        self._limit = n
        return self

    def _match(self, row: dict) -> bool:
        for kind, col, value in self._filters:
            if kind == "eq":
                if row.get(col) != value:
                    return False
            elif kind == "in":
                if row.get(col) not in value:
                    return False
            elif kind == "lt":
                left, right = _parse(row.get(col)), _parse(value)
                if left is None or right is None or not (left < right):
                    return False
            elif kind == "is":
                if (row.get(col) is None) != (value is None):
                    return False
        return True

    async def execute(self) -> SimpleNamespace:
        rows = [r for r in self._store.tables[self._table] if self._match(r)]
        if self._limit is not None:
            rows = rows[: self._limit]
        if self._mode == "update":
            for row in rows:
                row.update(self._values)
        self._store.query_calls.append((self._table, self._mode, tuple(self._filters)))
        return SimpleNamespace(data=rows)


class FakeStorageBucket:
    def __init__(self, store: "FakeStore", bucket: str) -> None:
        self._store = store
        self._bucket = bucket

    async def list(self, path: str | None = None) -> list[dict]:
        prefix = f"{path}/" if path else ""
        return [
            {"name": full.split("/")[-1], "path": full}
            for full in sorted(self._store.objects[self._bucket])
            if full.startswith(prefix)
        ]

    async def remove(self, paths: list[str]) -> list[dict]:
        if self._store.remove_error_buckets & {self._bucket}:
            raise RuntimeError("storage transport exploded")
        self._store.remove_calls.append((self._bucket, tuple(paths)))
        removed = []
        for full in paths:
            if full in self._store.objects[self._bucket]:
                del self._store.objects[self._bucket][full]
                removed.append({"Name": full})
        return removed


class FakeStorage:
    def __init__(self, store: "FakeStore") -> None:
        self._store = store

    def from_(self, bucket: str) -> FakeStorageBucket:
        return FakeStorageBucket(self._store, bucket)


class FakeStore:
    """In-memory stand-in for the Supabase client (tables + storage)."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {"jobs": [], "clips": []}
        self.objects: dict[str, dict] = {SOURCES_BUCKET: {}, CLIPS_BUCKET: {}}
        self.remove_calls: list[tuple[str, tuple[str, ...]]] = []
        self.query_calls: list[tuple[str, str, tuple]] = []
        self.remove_error_buckets: set[str] = set()

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    @property
    def storage(self) -> FakeStorage:
        return FakeStorage(self)


def make_store() -> FakeStore:
    store = FakeStore()
    store.tables["jobs"] = [
        {"id": JOB_A, "status": "completed", "source_type": "upload",
         "source_path": f"sources/{JOB_A}/original/abc.mp4",
         "expires_at": EXPIRED, "purged_at": None},
        {"id": JOB_B, "status": "failed", "source_type": "upload",
         "source_path": f"sources/{JOB_B}/original/def.mp4",
         "expires_at": FUTURE_DEADLINE, "purged_at": None},
        {"id": JOB_C, "status": "completed", "source_type": "upload",
         "source_path": f"sources/{JOB_C}/original/ghi.mp4",
         "expires_at": EXPIRED, "purged_at": ALREADY_PURGED_AT},
        {"id": JOB_D, "status": "rendering", "source_type": "upload",
         "source_path": f"sources/{JOB_D}/original/jkl.mp4",
         "expires_at": EXPIRED, "purged_at": None},
        {"id": JOB_E, "status": "completed", "source_type": "upload",
         "source_path": None, "expires_at": None, "purged_at": None},
    ]
    store.tables["clips"] = [
        # JOB_A's clip set: one canonical path, one FOREIGN job's path, one
        # without a path, one malformed (traversal-looking) path.
        {"id": CLIP_A1, "job_id": JOB_A, "status": "ready",
         "storage_path": f"clips/{JOB_A}/{CLIP_A1}.mp4"},
        {"id": CLIP_A2, "job_id": JOB_A, "status": "ready",
         "storage_path": f"clips/{JOB_B}/{CLIP_A2}.mp4"},
        {"id": CLIP_A3, "job_id": JOB_A, "status": "failed", "storage_path": None},
        {"id": CLIP_A4, "job_id": JOB_A, "status": "ready",
         "storage_path": f"clips/{JOB_A}/../../etc/passwd"},
    ]
    store.objects[SOURCES_BUCKET] = {
        f"sources/{JOB_A}/original/abc.mp4": b"source-bytes-a",
        f"sources/{JOB_B}/original/def.mp4": b"source-bytes-b",
        f"sources/{JOB_D}/original/jkl.mp4": b"source-bytes-d",
    }
    store.objects[CLIPS_BUCKET] = {
        f"clips/{JOB_A}/{CLIP_A1}.mp4": b"clip-bytes-a1",
        f"clips/{JOB_B}/{CLIP_A2}.mp4": b"clip-bytes-b-foreign",
    }
    return store


# -- discovery (retention policy) ---------------------------------------------------------


def test_list_purgeable_returns_only_expired_terminal_unpurged():
    service = PurgeService(make_store())
    ids = [row["id"] for row in run(service.list_purgeable_jobs(now=NOW))]
    # A: completed + expired + un-purged. B: not expired yet. C: already
    # purged. D: active (rendering). E: no deadline — never guessed.
    assert ids == [JOB_A]


def test_list_purgeable_excludes_deadline_exactly_at_now():
    store = make_store()
    for job in store.tables["jobs"]:
        if job["id"] == JOB_B:
            job["status"] = "completed"
            job["expires_at"] = "2026-11-13T00:00:00Z"  # == NOW (lt is strict)
    service = PurgeService(store)
    # JOB_A is still legitimately purgeable; JOB_B (deadline == NOW) is not (lt is strict).
    ids = [row["id"] for row in run(service.list_purgeable_jobs(now=NOW))]
    assert ids == [JOB_A]


# -- per-job purge: scope + marker ---------------------------------------------------------


def test_purge_deletes_job_scoped_objects_and_marks_purged():
    store = make_store()
    service = PurgeService(store)
    result = run(service.purge_job(JOB_A, now=NOW))

    assert result.ok
    assert result.sources_deleted == 1
    assert result.clips_deleted == 1
    assert result.marked_purged is True

    # Exactly the job's own objects were deleted — nothing else.
    assert store.remove_calls == [
        (SOURCES_BUCKET, (f"sources/{JOB_A}/original/abc.mp4",)),
        (CLIPS_BUCKET, (f"clips/{JOB_A}/{CLIP_A1}.mp4",)),
    ]
    # JOB_B's objects (and the foreign-path decoy) are untouched.
    assert f"sources/{JOB_B}/original/def.mp4" in store.objects[SOURCES_BUCKET]
    assert f"clips/{JOB_B}/{CLIP_A2}.mp4" in store.objects[CLIPS_BUCKET]

    # The DB marker was set (idempotency key for the next run).
    job_a = next(j for j in store.tables["jobs"] if j["id"] == JOB_A)
    assert job_a["purged_at"] is not None


def test_foreign_and_malformed_clip_paths_are_never_deleted():
    store = make_store()
    service = PurgeService(store)
    run(service.purge_job(JOB_A, now=NOW))
    deleted = [p for _bucket, paths in store.remove_calls for p in paths]
    assert f"clips/{JOB_B}/{CLIP_A2}.mp4" not in deleted
    assert all("../../" not in p for p in deleted)
    # The malformed row stays in the DB, un-deleted, for inspection.
    clip_a4 = next(c for c in store.tables["clips"] if c["id"] == CLIP_A4)
    assert clip_a4["storage_path"] == f"clips/{JOB_A}/../../etc/passwd"


def test_active_job_is_untouched():
    store = make_store()
    service = PurgeService(store)
    result = run(service.purge_job(JOB_D, now=NOW))
    assert not result.ok
    assert "not a purge candidate" in (result.error or "")
    assert store.remove_calls == []
    assert f"sources/{JOB_D}/original/jkl.mp4" in store.objects[SOURCES_BUCKET]


def test_invalid_job_id_never_reaches_the_db():
    store = make_store()
    service = PurgeService(store)
    result = run(service.purge_job("not-a-uuid", now=NOW))
    assert not result.ok
    assert store.query_calls == []
    assert store.remove_calls == []


def test_purged_job_is_not_a_candidate_anymore():
    store = make_store()
    service = PurgeService(store)
    result = run(service.purge_job(JOB_C, now=NOW))  # already purged
    assert not result.ok
    assert store.remove_calls == []


def test_mark_guard_refuses_when_deadline_was_extended():
    store = make_store()
    service = PurgeService(store)
    job_a = next(j for j in store.tables["jobs"] if j["id"] == JOB_A)
    job_a["expires_at"] = FUTURE_DEADLINE  # operator extended it
    marked = run(service._mark_purged(JOB_A, NOW))
    assert marked is False
    assert job_a["purged_at"] is None


# -- idempotency + partial failure ------------------------------------------------------------


def test_second_run_after_purge_is_a_noop():
    store = make_store()
    service = PurgeService(store)
    run(service.purge_job(JOB_A, now=NOW))
    calls_after_first = len(store.remove_calls)

    assert run(service.list_purgeable_jobs(now=NOW)) == []
    result = run(service.purge_job(JOB_A, now=NOW))
    assert not result.ok
    assert len(store.remove_calls) == calls_after_first  # nothing deleted twice


def test_partial_failure_retries_safely_without_double_delete():
    store = make_store()
    service = PurgeService(store)

    # Run 1: clip deletion fails AFTER the sources were deleted.
    store.remove_error_buckets.add(CLIPS_BUCKET)
    result1 = run(service.purge_job(JOB_A, now=NOW))
    assert not result1.ok
    assert result1.sources_deleted == 1
    assert result1.clips_deleted == 0
    assert result1.marked_purged is False  # NEVER marked while objects remain
    job_a = next(j for j in store.tables["jobs"] if j["id"] == JOB_A)
    assert job_a["purged_at"] is None

    # Run 2: transport recovered → finishes the job.
    store.remove_error_buckets.clear()
    result2 = run(service.purge_job(JOB_A, now=NOW))
    assert result2.ok
    assert result2.sources_deleted == 0  # sources already gone — no double delete
    assert result2.clips_deleted == 1
    assert result2.marked_purged is True

    sources_calls = [paths for bucket, paths in store.remove_calls if bucket == SOURCES_BUCKET]
    assert len(sources_calls) == 1
    assert f"clips/{JOB_A}/{CLIP_A1}.mp4" not in store.objects[CLIPS_BUCKET]


# -- manual CLI -------------------------------------------------------------------------------


class _FakeSettings:
    def __init__(self, configured: bool) -> None:
        self.supabase_configured = configured
        self.supabase_url = "http://localhost:54321"
        self.supabase_service_role_key = "service-role"


def test_cli_reports_unconfigured_and_exits_zero(monkeypatch):
    monkeypatch.setattr("app.config.settings.get_settings", lambda: _FakeSettings(False))
    assert run(run_cleanup()) == 0


def test_cli_nothing_to_do(monkeypatch):
    store = make_store()
    for job in store.tables["jobs"]:
        job["purged_at"] = ALREADY_PURGED_AT
    monkeypatch.setattr("app.config.settings.get_settings", lambda: _FakeSettings(True))
    monkeypatch.setattr("supabase.create_async_client", lambda url, key: store)
    assert run(run_cleanup()) == 0


def test_cli_successful_pass(monkeypatch, capsys):
    store = make_store()
    monkeypatch.setattr("app.config.settings.get_settings", lambda: _FakeSettings(True))
    monkeypatch.setattr("supabase.create_async_client", lambda url, key: store)
    assert run(run_cleanup()) == 0
    assert "marked purged" in capsys.readouterr().out


def test_cli_exit_code_signals_partial_failure(monkeypatch, capsys):
    store = make_store()
    store.remove_error_buckets.add(CLIPS_BUCKET)
    monkeypatch.setattr("app.config.settings.get_settings", lambda: _FakeSettings(True))
    monkeypatch.setattr("supabase.create_async_client", lambda url, key: store)
    assert run(run_cleanup()) == 1
    assert "INCOMPLETE" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["../../etc", "clips/x/y.mp4", "", "UPPER"])
def test_uuid_gate_rejects_non_uuids(bad):
    service = PurgeService(make_store())
    result = run(service.purge_job(bad, now=NOW))
    assert not result.ok
    assert result.error == "invalid job id"
