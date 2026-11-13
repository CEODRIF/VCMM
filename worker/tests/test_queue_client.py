"""SupabaseQueue client tests (mocked transport, real mapping logic)."""

from __future__ import annotations

import asyncio

import pytest

from app.queue.supabase_queue import QueueError, SupabaseQueue


class FakeQueryBuilder:
    """Records the (table, action, values, filters, select) tuple."""

    def __init__(self, client: "FakeTableClient", table: str, action: str, values: dict | None) -> None:
        self._client = client
        self.table = table
        self.action = action
        self.values = values
        self.filters: dict[str, object] = {}
        self.select_cols: str | None = None
        self.limit_value: int | None = None

    def eq(self, column: str, value):
        self.filters[column] = value
        return self

    def select(self, columns: str):
        self.select_cols = columns
        return self

    def limit(self, n: int):
        self.limit_value = n
        return self

    def __await__(self):
        return self._client._run(
            self.table, self.action, self.values, self.filters, self.select_cols, self.limit_value
        ).__await__()


class FakeTableClient:
    def __init__(self, fake: "FakeClient") -> None:
        self._fake = fake

    def table(self, name: str):
        return _TableProxy(self, name)

    async def _run(self, table, action, values, filters, select, limit):
        if (table, action) in self._fake.raise_on:
            raise RuntimeError(f"transport exploded: {self._fake.raise_on[(table, action)]}")
        self._fake.table_calls.append((table, action, dict(values or {}), dict(filters), select, limit))
        key = (table, action)
        if key in self._fake.responses:
            payload = self._fake.responses[key]
            if isinstance(payload, list):
                rows = payload
            else:
                rows = [payload]
            return [row for row in rows][: limit or len(rows)]
        return []


class _TableProxy:
    def __init__(self, owner: FakeTableClient, table: str) -> None:
        self._owner = owner
        self._table = table

    def update(self, values: dict) -> FakeQueryBuilder:
        return FakeQueryBuilder(self._owner, self._table, "update", values)

    def select(self, columns: str) -> FakeQueryBuilder:
        builder = FakeQueryBuilder(self._owner, self._table, "select", None)
        builder.select_cols = columns
        return builder


class FakeClient:
    def __init__(self, responses: dict | None = None, raise_on: dict | None = None) -> None:
        self.responses = responses or {}
        self.raise_on = raise_on or {}
        self.calls: list[tuple[str, dict]] = []
        self.table_calls: list[tuple[str, str, dict, dict, str | None, int | None]] = []

    async def rpc(self, name: str, params: dict):
        self.calls.append((name, params))
        if name in self.raise_on:
            raise RuntimeError(f"transport exploded: {self.raise_on[name]}")
        return self.responses.get(name)

    def table(self, name: str):
        return FakeTableClient(self).table(name)


def run(coro):
    return asyncio.run(coro)


def test_claim_next_job_maps_parameters_and_returns_row():
    client = FakeClient(responses={"claim_next_job": {"id": "j1", "status": "validating"}})
    queue = SupabaseQueue(client)
    job = run(queue.claim_next_job("w1", 300))
    assert job == {"id": "j1", "status": "validating"}
    assert client.calls == [("claim_next_job", {"p_worker_id": "w1", "p_lease_seconds": 300})]


def test_claim_next_job_returns_none_when_queue_is_empty():
    queue = SupabaseQueue(FakeClient())
    assert run(queue.claim_next_job("w1", 300)) is None


def test_renew_and_progress_coerce_booleans():
    client = FakeClient(responses={"renew_job_lease": True, "update_job_progress": False, "release_job": None})
    queue = SupabaseQueue(client)
    assert run(queue.renew_job_lease("j1", "w1", 300)) is True
    assert run(queue.update_job_progress("j1", "w1", 42)) is False
    assert run(queue.release_job("j1", "w1")) is False
    names = [call[0] for call in client.calls]
    assert names == ["renew_job_lease", "update_job_progress", "release_job"]


def test_complete_job_ingestion_sends_metadata():
    client = FakeClient(responses={"complete_job_ingestion": True})
    queue = SupabaseQueue(client)
    assert run(queue.complete_job_ingestion("j1", "w1", {"container": "mp4"})) is True
    assert client.calls[0][1] == {
        "p_job_id": "j1",
        "p_worker_id": "w1",
        "p_source_metadata": {"container": "mp4"},
    }


def test_complete_job_rendering_maps_parameters_and_coerces_boolean():
    """Phase 5: the ONLY terminal-completion path — the DB guard lives in
    the RPC; the client just maps parameters and coerces the boolean."""
    client = FakeClient(responses={"complete_job_rendering": True})
    queue = SupabaseQueue(client)
    assert run(queue.complete_job_rendering("j1", "w1")) is True
    assert client.calls == [("complete_job_rendering", {"p_job_id": "j1", "p_worker_id": "w1"})]

    refused = SupabaseQueue(FakeClient(responses={"complete_job_rendering": False}))
    assert run(refused.complete_job_rendering("j1", "w1")) is False


def test_fail_job_defaults_to_non_permanent_and_returns_new_status():
    client = FakeClient(responses={"fail_job": "queued"})
    queue = SupabaseQueue(client)
    status = run(queue.fail_job("j1", "w1", "storage_download", "timeout"))
    assert status == "queued"
    params = client.calls[0][1]
    assert params["p_permanent"] is False
    assert params["p_error_category"] == "storage_download"


def test_fail_job_maps_none_response_to_not_owner():
    queue = SupabaseQueue(FakeClient())
    assert run(queue.fail_job("j1", "w1", "unknown", "x")) == "not_owner"


def test_transport_failure_raises_queue_error_without_leaking_details():
    # A transport error must surface as QueueError with the RPC name only —
    # never the raw payload (which could contain user data).
    queue = SupabaseQueue(FakeClient(raise_on={"claim_next_job": "sensitive-details"}))
    with pytest.raises(QueueError) as exc:
        run(queue.claim_next_job("w1", 300))
    message = str(exc.value)
    assert "claim_next_job" in message
    assert "sensitive-details" not in message
    assert "RuntimeError" in message


# ---- Phase 4: guarded direct updates (transition + parking) -----------------

def test_transition_stage_updates_only_when_owner_and_stage_match():
    client = FakeClient(responses={("jobs", "update"): [{"id": "j1", "status": "analyzing"}]})
    queue = SupabaseQueue(client)
    assert run(queue.transition_stage("j1", "w1", "transcribing", "analyzing")) == "transitioned"
    table, action, values, filters, select, _limit = client.table_calls[0]
    assert (table, action) == ("jobs", "update")
    assert values == {"status": "analyzing"}
    # Ownership + stage scoping: a stale owner or a moved job matches 0 rows.
    assert filters == {"id": "j1", "status": "transcribing", "locked_by": "w1"}
    assert select == "id,status,locked_by"


def test_transition_stage_reports_already_when_crash_recovery_resumes():
    # Update matched nothing (job was already 'analyzing'), but the follow-up
    # select shows we still own it in the target stage.
    client = FakeClient(responses={
        ("jobs", "update"): [],
        ("jobs", "select"): {"id": "j1", "status": "analyzing", "locked_by": "w1"},
    })
    queue = SupabaseQueue(client)
    assert run(queue.transition_stage("j1", "w1", "transcribing", "analyzing")) == "already"
    assert [c[1] for c in client.table_calls] == ["update", "select"]


def test_transition_stage_reports_not_owner_when_someone_else_moved_it():
    client = FakeClient(responses={
        ("jobs", "update"): [],
        ("jobs", "select"): {"id": "j1", "status": "analyzing", "locked_by": "w2"},
    })
    queue = SupabaseQueue(client)
    assert run(queue.transition_stage("j1", "w1", "transcribing", "analyzing")) == "not_owner"
    client2 = FakeClient(responses={
        ("jobs", "update"): [],
        ("jobs", "select"): {"id": "j1", "status": "failed", "locked_by": None},
    })
    assert run(SupabaseQueue(client2).transition_stage("j1", "w1", "transcribing", "analyzing")) == "not_owner"
    client3 = FakeClient(responses={("jobs", "update"): [], ("jobs", "select"): None})
    assert run(SupabaseQueue(client3).transition_stage("j1", "w1", "transcribing", "analyzing")) == "not_owner"


def test_park_after_analysis_clears_lock_on_analyzing_job_only():
    client = FakeClient(responses={("jobs", "update"): [{"id": "j1"}]})
    queue = SupabaseQueue(client)
    assert run(queue.park_after_analysis("j1", "w1")) is True
    table, action, values, filters, select, _limit = client.table_calls[0]
    assert (table, action) == ("jobs", "update")
    assert values == {"locked_by": None, "locked_at": None, "lease_expires_at": None, "progress": 100}
    assert filters == {"id": "j1", "status": "analyzing", "locked_by": "w1"}
    assert select == "id"


def test_park_after_analysis_false_when_not_owner():
    client = FakeClient(responses={("jobs", "update"): []})
    queue = SupabaseQueue(client)
    assert run(queue.park_after_analysis("j1", "w1")) is False


def test_phase4_table_transport_errors_are_queue_errors():
    client = FakeClient(raise_on={("jobs", "update"): "boom-details"})
    queue = SupabaseQueue(client)
    with pytest.raises(QueueError) as exc:
        run(queue.park_after_analysis("j1", "w1"))
    assert "jobs" in str(exc.value)
    assert "boom-details" not in str(exc.value)
