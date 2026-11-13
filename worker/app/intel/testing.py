"""In-memory reference store for the intel checkpoint tables (TASK 007).

A supabase.Client-shaped FAKE with the real upsert/dedup semantics of the
two Phase 7 tables. Used by the unit tests, the pipeline recovery tests and
the deterministic benchmark runner — NO live database is ever touched.

This module is a TEST/BENCHMARK double, not a production code path: the
worker always uses the real Supabase service-role client.
"""

from __future__ import annotations

from typing import Any

CHECKPOINTS = "processing_checkpoints"
EVENTS = "candidate_events"


class FakeResult:
    def __init__(self, data: list[Any]):
        self.data = data


class _Builder:
    def __init__(self, backend: "FakeIntelBackend", table: str, action: str,
                 payload: Any = None):
        self._b = backend
        self._table = table
        self._action = action
        self._payload = payload
        self._filters: dict[str, Any] = {}
        self._order: str | None = None

    def select(self, cols: str) -> "_Builder":
        b = _Builder(self._b, self._table, "select")
        b._filters = dict(self._filters)
        b._order = self._order
        return b

    def upsert(self, rows: Any, on_conflict: str | None = None,
               ignore_duplicates: bool = False) -> "_Builder":
        self._action = "upsert"
        self._payload = (rows, on_conflict, ignore_duplicates)
        return self

    def delete(self) -> "_Builder":
        self._action = "delete"
        return self

    def eq(self, k: str, v: Any) -> "_Builder":
        self._filters[k] = v
        return self

    def order(self, k: str) -> "_Builder":
        self._order = k
        return self

    async def execute(self) -> FakeResult:
        self._b.calls.append((self._table, self._action, self._payload,
                              dict(self._filters), self._order))
        if self._b.fail_action == self._action:
            raise RuntimeError("transport exploded: SECRET-INTERNAL-DETAIL")
        if self._action == "select":
            job = self._filters.get("job_id")
            store = self._b.checkpoints if self._table == CHECKPOINTS else self._b.events
            rows = list(store.get(job, {}).values())
            if self._order == "start_time":
                rows = sorted(rows, key=lambda r: r["start_time"])
            return FakeResult(rows)
        if self._action == "upsert":
            rows, _on_conflict, ignore_duplicates = self._payload
            rows = rows if isinstance(rows, list) else [rows]
            store = self._b.checkpoints if self._table == CHECKPOINTS else self._b.events
            for row in rows:
                bucket = store.setdefault(row["job_id"], {})
                if self._table == CHECKPOINTS:
                    bucket[int(row["chunk_index"])] = row  # last write wins
                elif not (ignore_duplicates and row["event_key"] in bucket):
                    bucket[row["event_key"]] = row         # exact-dedup: keep first
            return FakeResult(rows)
        if self._action == "delete":
            job = self._filters.get("job_id")
            if self._table == CHECKPOINTS:
                self._b.checkpoints.pop(job, None)
            else:
                self._b.events.pop(job, None)
            return FakeResult([])
        raise AssertionError(f"unexpected fake action {self._action}")


class _Table:
    def __init__(self, backend: FakeIntelBackend, name: str):
        self._b = backend
        self._name = name

    def select(self, cols: str) -> _Builder:
        return _Builder(self._b, self._name, "select")

    def upsert(self, rows: Any, on_conflict: str | None = None,
               ignore_duplicates: bool = False) -> _Builder:
        return _Builder(self._b, self._name, "upsert",
                        (rows, on_conflict, ignore_duplicates))

    def delete(self) -> _Builder:
        return _Builder(self._b, self._name, "delete")


class FakeIntelBackend:
    """In-memory stand-in for the two Phase 7 tables (job-scoped buckets)."""

    def __init__(self) -> None:
        self.checkpoints: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_action: str | None = None  # set to an action to simulate a transport error


class FakeSupabaseClient:
    """supabase.Client-shaped object backed by a FakeIntelBackend."""

    def __init__(self, fail_action: str | None = None) -> None:
        self.backend = FakeIntelBackend()
        self.backend.fail_action = fail_action

    @property
    def calls(self) -> list[tuple]:
        return self.backend.calls

    def table(self, name: str) -> _Table:
        assert name in (CHECKPOINTS, EVENTS)
        return _Table(self.backend, name)
