"""Checkpoint + candidate-event store tests (TASK 007) — stateful fake
Supabase client. Proves persistence, recovery, idempotency, job scoping and
failure handling WITHOUT any live database.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from app.intel.checkpoints import CHECKPOINTS_TABLE, EVENTS_TABLE, IntelStore, IntelStoreError
from app.intel.signals import KIND_ENERGY_SPIKE, KIND_TRANSIENT, SignalEvent
from intel_fixtures import FakeSupabaseClient

JOB = str(UUID("11111111-1111-4111-8111-111111111111"))
OTHER_JOB = str(UUID("99999999-9999-4999-8999-999999999999"))


def run(coro):
    return asyncio.run(coro)


def _store(fail_action=None) -> tuple[IntelStore, FakeSupabaseClient]:
    client = FakeSupabaseClient(fail_action)
    return IntelStore(client), client


def _ev(t, kind=KIND_ENERGY_SPIKE, strength=0.9):
    return SignalEvent(t=t, t_end=t + 0.5, family="audio", kind=kind, strength=strength,
                       evidence={"family": "audio", "kind": kind, "strength": strength})


# ---- checkpoints: persistence + recovery + idempotency -------------------------------

def test_mark_chunk_done_is_idempotent_upsert():
    store, client = _store()
    feats = {"family": "audio", "rms_1hz": [0.1, 0.2]}
    run(store.mark_chunk_done(JOB, 3, 1500.0, 2100.0, feats))
    run(store.mark_chunk_done(JOB, 3, 1500.0, 2100.0, feats))  # retry after "crash"
    done = run(store.load_done_chunks(JOB))
    assert set(done) == {3}
    row = done[3]
    assert row["start_time"] == 1500.0
    assert row["end_time"] == 2100.0
    assert row["features"] == feats
    # both upserts carried only this job's row
    upserts = [c for c in client.calls if c[1] == "upsert"]
    assert len(upserts) == 2
    for _table, _action, payload, _filters, _order in upserts:
        rows = payload[0] if isinstance(payload[0], list) else [payload[0]]
        assert all(r["job_id"] == JOB for r in rows)


def test_load_done_chunks_recovery_and_isolation():
    store, client = _store()
    run(store.mark_chunk_done(JOB, 0, 0.0, 600.0, {"a": 1}))
    run(store.mark_chunk_done(JOB, 1, 570.0, 1170.0, {"a": 2}))
    run(store.mark_chunk_done(OTHER_JOB, 0, 0.0, 600.0, {"a": 9}))
    mine = run(store.load_done_chunks(JOB))
    theirs = run(store.load_done_chunks(OTHER_JOB))
    assert set(mine) == {0, 1}
    assert mine[1]["features"] == {"a": 2}
    assert set(theirs) == {0}  # a stale run can never see another job's progress


def test_no_done_chunks_is_empty_dict():
    store, _ = _store()
    assert run(store.load_done_chunks(JOB)) == {}


# ---- events: exact-dedup + ordering ----------------------------------------------------

def test_upsert_events_exact_dedup_on_retry():
    store, client = _store()
    e1 = _ev(100.0)
    e2 = _ev(200.0, kind=KIND_TRANSIENT)
    # First pass: 2 events. "Crash" before checkpoint → resume re-detects the
    # SAME physical signals (slightly jittered start time → same 0.1 s bucket).
    assert run(store.upsert_events(JOB, [e1, e2])) == 2
    jittered = SignalEvent(t=100.04, t_end=100.5, family="audio", kind=KIND_ENERGY_SPIKE,
                           strength=0.9, evidence=e1.evidence)
    assert run(store.upsert_events(JOB, [jittered, e2])) == 2
    loaded = run(store.load_events(JOB))
    assert len(loaded) == 2  # NO duplicates created by the retry
    assert [e.t for e in loaded] == [100.0, 200.0]  # time-ordered


def test_upsert_events_empty_is_noop():
    store, client = _store()
    assert run(store.upsert_events(JOB, [])) == 0
    assert client.calls == []


def test_events_scoped_per_job():
    store, _ = _store()
    run(store.upsert_events(JOB, [_ev(10.0)]))
    run(store.upsert_events(OTHER_JOB, [_ev(20.0)]))
    assert [e.t for e in run(store.load_events(JOB))] == [10.0]
    assert [e.t for e in run(store.load_events(OTHER_JOB))] == [20.0]


# ---- job-id hardening -------------------------------------------------------------------

def test_non_uuid_job_id_rejected_before_any_query():
    for bad in ("", None, "not-a-uuid", "12345", "drop table processing_checkpoints;--"):
        store, client = _store()
        with pytest.raises(ValueError):
            run(store.load_done_chunks(bad))  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            run(store.upsert_events(bad, [_ev(1.0)]))  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            run(store.mark_chunk_done(bad, 0, 0.0, 1.0, {}))  # type: ignore[arg-type]
        assert client.calls == []  # nothing leaked to the transport


# ---- reset (scoped delete) ----------------------------------------------------------------

def test_reset_for_job_deletes_only_that_job():
    store, _ = _store()
    run(store.upsert_events(JOB, [_ev(10.0)]))
    run(store.mark_chunk_done(JOB, 0, 0.0, 600.0, {"x": 1}))
    run(store.upsert_events(OTHER_JOB, [_ev(20.0)]))
    run(store.mark_chunk_done(OTHER_JOB, 0, 0.0, 600.0, {"x": 2}))

    run(store.reset_for_job(JOB))

    assert run(store.load_events(JOB)) == []
    assert run(store.load_done_chunks(JOB)) == {}
    assert [e.t for e in run(store.load_events(OTHER_JOB))] == [20.0]  # untouched
    assert set(run(store.load_done_chunks(OTHER_JOB))) == {0}


# ---- failure handling (safe errors, no internals leaked) ----------------------------------

def test_store_wraps_transport_errors_safely():
    for action, fn in (
        ("select", lambda s: s.load_done_chunks(JOB)),
        ("upsert", lambda s: s.mark_chunk_done(JOB, 0, 0.0, 1.0, {})),
        ("delete", lambda s: s.reset_for_job(JOB)),
    ):
        store, _ = _store(fail_action=action)
        with pytest.raises(IntelStoreError) as exc:
            run(fn(store))
        assert "SECRET-INTERNAL-DETAIL" not in str(exc.value)  # no internals in the message


def test_event_upsert_failure_is_safe():
    store, _ = _store(fail_action="upsert")
    with pytest.raises(IntelStoreError):
        run(store.upsert_events(JOB, [_ev(1.0)]))
