"""Clip store transport tests (Phase 4 save + Phase 5 list/mark) with a
mocked supabase client. Every query must be scoped so a stale worker can
never touch another job's rows.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from app.ai.clip_detection import ScoredClip
from app.storage.clip_store import CLIPS_TABLE, ClipsPersistenceError, SupabaseClipStore

JOB = str(UUID("11111111-1111-4111-8111-111111111111"))
OTHER_JOB = str(UUID("99999999-9999-4999-8999-999999999999"))
CLIP = str(UUID("22222222-2222-4222-8222-222222222222"))


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeBuilder:
    def __init__(self, owner, action, payload):
        self._owner = owner
        self.action = action
        self.payload = payload
        self.filters: dict[str, object] = {}
        self.select_cols = None
        self.order_col = None

    def select(self, cols):
        self.select_cols = cols
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def order(self, column):
        self.order_col = column
        return self

    async def execute(self):
        self._owner.calls.append((self.action, self.payload, dict(self.filters), self.select_cols, self.order_col))
        if self._owner.raise_on_action == self.action:
            raise RuntimeError("transport exploded")
        return FakeResult(self._owner.results.get(self.action, []))


class FakeTable:
    def __init__(self, owner, name):
        self._owner, self.name = owner, name

    def delete(self):
        return FakeBuilder(self._owner, "delete", None)

    def insert(self, rows):
        return FakeBuilder(self._owner, "insert", rows)

    def select(self, cols):
        b = FakeBuilder(self._owner, "select", None)
        b.select_cols = cols
        return b

    def update(self, values):
        return FakeBuilder(self._owner, "update", values)


class FakeClient:
    def __init__(self, results=None, raise_on_action=None):
        self.results = results or {}
        self.raise_on_action = raise_on_action
        self.calls: list[tuple] = []

    def table(self, name):
        assert name == CLIPS_TABLE
        return FakeTable(self, name)


def run(coro):
    return asyncio.run(coro)


# ---- Phase 4 regression: save_clips --------------------------------------------

def test_save_clips_delete_then_insert_scoped_to_job():
    client = FakeClient()
    store = SupabaseClipStore(client)
    clips = [
        ScoredClip(start=60.0, end=120.0, title="Intro", score=88.0,
                   scoring={"engagement": 90.0, "pacing": 80.0}),
        ScoredClip(start=120.0, end=180.0, title="Hook", score=74.0,
                   scoring={"engagement": 70.0, "pacing": 85.0}),
    ]
    count = run(store.save_clips(JOB, clips))
    assert count == 2
    assert len(client.calls) == 2
    delete_action, delete_payload, delete_filters, _, _ = client.calls[0]
    assert delete_action == "delete"
    assert delete_filters == {"job_id": JOB}
    insert_action, insert_rows, insert_filters, _, _ = client.calls[1]
    assert insert_action == "insert"
    assert len(insert_rows) == 2
    row = insert_rows[0]
    assert row["job_id"] == JOB
    assert row["status"] == "pending"
    assert row["storage_path"] is None
    assert row["sort_order"] == 0
    assert insert_rows[1]["sort_order"] == 1


def test_save_clips_rejects_non_uuid_job():
    store = SupabaseClipStore(FakeClient())
    with pytest.raises(ValueError):
        run(store.save_clips("not-a-uuid", []))


# ---- Phase 5: list_clips ----------------------------------------------------------

def test_list_clips_scoped_and_ordered():
    rows = [{"id": CLIP, "job_id": JOB, "status": "pending"}]
    client = FakeClient(results={"select": rows})
    store = SupabaseClipStore(client)
    result = run(store.list_clips(JOB))
    assert result == rows
    assert len(client.calls) == 1
    action, _, filters, select_cols, order_col = client.calls[0]
    assert action == "select"
    assert filters == {"job_id": JOB}
    assert order_col == "sort_order"
    assert "status" in select_cols and "storage_path" in select_cols


def test_list_clips_empty_is_not_an_error():
    client = FakeClient(results={"select": []})
    store = SupabaseClipStore(client)
    assert run(store.list_clips(JOB)) == []


def test_list_clips_rejects_non_uuid_job():
    store = SupabaseClipStore(FakeClient())
    with pytest.raises(ValueError):
        run(store.list_clips("bad"))


def test_list_clips_transport_error_wrapped():
    store = SupabaseClipStore(FakeClient(raise_on_action="select"))
    with pytest.raises(ClipsPersistenceError):
        run(store.list_clips(JOB))


# ---- Phase 5: mark_clip -------------------------------------------------------------

def test_mark_clip_scoped_to_id_and_job():
    client = FakeClient(results={"update": [{"id": CLIP}]})
    store = SupabaseClipStore(client)
    ok = run(store.mark_clip(JOB, CLIP, "ready", storage_path=f"clips/{JOB}/{CLIP}.mp4"))
    assert ok is True
    action, values, filters, _, _ = client.calls[0]
    assert action == "update"
    assert values == {"status": "ready", "storage_path": f"clips/{JOB}/{CLIP}.mp4"}
    assert filters == {"id": CLIP, "job_id": JOB}


def test_mark_clip_processing_has_no_storage_path():
    client = FakeClient(results={"update": [{"id": CLIP}]})
    store = SupabaseClipStore(client)
    run(store.mark_clip(JOB, CLIP, "processing"))
    _, values, _, _, _ = client.calls[0]
    assert values == {"status": "processing"}  # storage_path untouched


def test_mark_clip_returns_false_when_row_not_owned():
    """A stale worker marking another job's clip updates 0 rows → False (the
    caller must treat this as lost ownership)."""
    client = FakeClient(results={"update": []})
    store = SupabaseClipStore(client)
    assert run(store.mark_clip(OTHER_JOB, CLIP, "ready")) is False


def test_mark_clip_rejects_unknown_status():
    store = SupabaseClipStore(FakeClient())
    for bad in ("complete", "READY", "rendering", ""):
        with pytest.raises(ValueError):
            run(store.mark_clip(JOB, CLIP, bad))


def test_mark_clip_rejects_non_uuid_ids():
    store = SupabaseClipStore(FakeClient())
    with pytest.raises(ValueError):
        run(store.mark_clip(JOB, "bad-clip", "ready"))
    with pytest.raises(ValueError):
        run(store.mark_clip("bad-job", CLIP, "ready"))


def test_mark_clip_transport_error_wrapped():
    store = SupabaseClipStore(FakeClient(raise_on_action="update"))
    with pytest.raises(ClipsPersistenceError):
        run(store.mark_clip(JOB, CLIP, "ready"))
