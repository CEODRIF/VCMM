"""SupabaseQueue client tests (mocked transport, real mapping logic)."""

from __future__ import annotations

import asyncio

import pytest

from app.queue.supabase_queue import QueueError, SupabaseQueue


class FakeClient:
    def __init__(self, responses: dict | None = None, raise_on: dict | None = None) -> None:
        self.responses = responses or {}
        self.raise_on = raise_on or {}
        self.calls: list[tuple[str, dict]] = []

    async def rpc(self, name: str, params: dict):
        self.calls.append((name, params))
        if name in self.raise_on:
            raise RuntimeError(f"transport exploded: {self.raise_on[name]}")
        return self.responses.get(name)


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
