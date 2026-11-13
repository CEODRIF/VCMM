"""Signal event model tests (TASK 007) — pure data, no I/O.

The event model is the anti-fabrication contract: every event must carry
measured evidence, and the stable ``event_key`` is what makes retries
exact-deduplicate instead of creating duplicate highlights.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.intel.signals import (
    SIGNAL_FAMILIES,
    event_key,
    from_row,
    SignalEvent,
)

JOB = str(UUID("11111111-1111-4111-8111-111111111111"))
OTHER_JOB = str(UUID("99999999-9999-4999-8999-999999999999"))


def _event(t=10.0, t_end=10.5, family="audio", kind="energy_spike", strength=0.8):
    return SignalEvent(t=t, t_end=t_end, family=family, kind=kind, strength=strength,
                       evidence={"family": family, "kind": kind, "strength": strength})


def test_event_key_is_stable_across_re_detection():
    a = _event(t=10.0)
    b = _event(t=10.04)  # same 0.1 s bucket → same physical signal
    assert event_key(JOB, a) == event_key(JOB, b)


def test_event_key_changes_when_signal_differs():
    a = _event(t=10.0)
    b = _event(t=10.14)  # next bucket
    c = _event(t=10.0, kind="transient")
    d = _event(t=10.0, family="visual", kind="scene_change")
    assert event_key(JOB, a) != event_key(JOB, b)
    assert event_key(JOB, a) != event_key(JOB, c)
    assert event_key(JOB, a) != event_key(JOB, d)


def test_event_key_scopes_to_job():
    a = _event()
    assert event_key(JOB, a) != event_key(OTHER_JOB, a)


def test_event_key_shape():
    key = event_key(JOB, _event())
    assert len(key) == 32
    int(key, 16)  # hex


def test_validation_rejects_bad_events():
    with pytest.raises(ValueError):
        SignalEvent(t=0.0, t_end=1.0, family="facecam", kind="x", strength=0.5)
    with pytest.raises(ValueError):
        SignalEvent(t=1.0, t_end=0.5, family="audio", kind="x", strength=0.5)
    with pytest.raises(ValueError):
        SignalEvent(t=-0.1, t_end=0.5, family="audio", kind="x", strength=0.5)
    with pytest.raises(ValueError):
        SignalEvent(t=0.0, t_end=1.0, family="audio", kind="x", strength=1.5)
    with pytest.raises(ValueError):
        SignalEvent(t=0.0, t_end=1.0, family="audio", kind="x", strength=-0.1)


def test_signal_families_are_open_but_validated():
    assert "audio" in SIGNAL_FAMILIES
    assert "visual" in SIGNAL_FAMILIES


def test_to_row_carries_evidence_and_defaults():
    e = _event(t=1000.0, t_end=1000.5, strength=0.9, kind="transient")
    e = SignalEvent(t=1000.0, t_end=1000.5, family="audio", kind="transient", strength=0.9,
                    evidence={"family": "audio", "kind": "transient", "strength": 0.9,
                              "zcr": 0.31, "peak_rms": 0.4})
    row = e.to_row(JOB)
    assert row["job_id"] == JOB
    assert row["event_key"] == event_key(JOB, e)
    assert row["start_time"] == 1000.0
    assert row["end_time"] == 1000.5
    assert row["category"] == "unclassified"  # fusion fills the real score later
    assert row["score"] == 0
    assert row["confidence"] == 0
    assert row["evidence"]["family"] == "audio"
    assert row["evidence"]["kind"] == "transient"
    assert row["evidence"]["strength"] == 0.9
    assert row["evidence"]["zcr"] == 0.31  # measured detail survives the round trip


def test_from_row_round_trip():
    e = _event(t=42.05, t_end=42.6, kind="motion_burst", family="visual", strength=0.77)
    row = e.to_row(JOB)
    back = from_row(row)
    assert back.t == pytest.approx(42.05)
    assert back.t_end == pytest.approx(42.6)
    assert back.family == "visual"
    assert back.kind == "motion_burst"
    assert back.strength == pytest.approx(0.77)
    # The re-materialized event has the SAME dedup identity.
    assert event_key(JOB, back) == event_key(JOB, e)
