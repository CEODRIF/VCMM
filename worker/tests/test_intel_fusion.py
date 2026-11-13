"""Multimodal fusion tests (TASK 007) — the anti-loud-alone contract.

THE rule under test: **a loud sound alone must not qualify**. A single
family is discounted, and a group whose only audio evidence is a transient
(game SFX — not a vocal reaction) can never reach the bar alone.
Qualification comes from INDEPENDENT families agreeing in time.
"""

from __future__ import annotations

import pytest

from app.intel.fusion import FusionConfig, fuse, score_cluster
from app.intel.signals import (
    KIND_ENERGY_SPIKE,
    KIND_MOTION_BURST,
    KIND_SCENE_CHANGE,
    KIND_TRANSIENT,
    SignalEvent,
)


def ev(t, family, kind, strength=1.0, t_end=None):
    return SignalEvent(t=t, t_end=t_end if t_end is not None else t + 0.3,
                       family=family, kind=kind, strength=strength,
                       evidence={"family": family, "kind": kind, "strength": strength})


# ---- THE rule ----------------------------------------------------------------------

def test_loud_transient_alone_never_qualifies():
    # A maximum-strength loud game-SFX burst, by itself.
    events = [ev(100.0, "audio", KIND_TRANSIENT, strength=1.0)]
    assert fuse(events) == []  # nothing qualifies

    # Even many loud transients in the same 2.5 s window (one cluster).
    burst = [ev(100.0 + i * 0.4, "audio", KIND_TRANSIENT, strength=1.0) for i in range(5)]
    assert fuse(burst) == []


def test_loud_loud_loud_still_never_qualifies():
    # Sanity: the SFX-only solo factor (0.2 × 100 × max_strength) can't
    # reach min_score 25 for any strength in [0, 1].
    for strength in (0.0, 0.25, 0.5, 0.75, 1.0):
        score, conf, reasons = score_cluster(
            [ev(0.0, "audio", KIND_TRANSIENT, strength=strength)], FusionConfig()
        )
        assert reasons["solo_mode"] == "sfx_only_transient"
        assert score < FusionConfig().min_score
        assert conf == 0.5


def test_two_families_qualifying():
    # Same moment: loud burst (audio) + measured cut (visual) → qualifies,
    # full confidence, diversity bonus applied.
    events = [
        ev(100.0, "audio", KIND_TRANSIENT, strength=1.0),
        ev(100.2, "visual", KIND_SCENE_CHANGE, strength=1.0),
    ]
    windows = fuse(events)
    assert len(windows) == 1
    w = windows[0]
    assert w.qualifies
    # weighted = (0.5·1 + 0.5·1)/1.0 = 1.0 → 100 × 1.0 × 1.25 = 125 → capped 100
    assert w.score == 100.0
    assert w.confidence == 1.0
    assert w.families == ("audio", "visual")
    assert w.reasons["solo_mode"] is None


def test_vocal_plus_scene_qualifies_with_temporal_order():
    # event → reaction → aftermath stays in time order inside the window.
    events = [
        ev(100.0, "visual", KIND_SCENE_CHANGE, strength=0.9),   # the kill
        ev(100.5, "audio", KIND_ENERGY_SPIKE, strength=1.0),    # the shout
        ev(101.0, "audio", KIND_ENERGY_SPIKE, strength=0.6),    # the aftermath
    ]
    windows = fuse(events)
    assert len(windows) == 1
    w = windows[0]
    assert w.qualifies and w.score >= 50
    assert [e.t for e in w.events] == sorted(e.t for e in w.events)
    assert w.start == pytest.approx(100.0)
    assert w.end == pytest.approx(101.3)  # max t_end preserved


def test_single_strong_vocal_family_may_qualify():
    # A very strong VOICE signal (not SFX) is a legitimate solo candidate —
    # discounted (×0.5) but over the bar at high strength.
    events = [ev(50.0, "audio", KIND_ENERGY_SPIKE, strength=1.0)]
    windows = fuse(events)
    assert len(windows) == 1
    assert windows[0].score == 50.0
    assert windows[0].reasons["solo_mode"] == "single_family"
    # But a weak voice signal does not:
    weak = [ev(50.0, "audio", KIND_ENERGY_SPIKE, strength=0.2)]
    assert fuse(weak) == []  # 100·0.2·0.5 = 10 < 25


def test_scattered_events_do_not_cluster():
    # 4 s apart > 2.5 s window → separate clusters, no fake "multi-family".
    events = [
        ev(100.0, "audio", KIND_TRANSIENT, strength=1.0),
        ev(104.0, "visual", KIND_SCENE_CHANGE, strength=1.0),
    ]
    windows = fuse(events)
    # Neither qualifies alone (sfx-only 20 < 25; visual solo 50 ≥ 25 → 1 window)
    assert len(windows) == 1
    assert windows[0].families == ("visual",)


def test_chain_clustering_within_window():
    # a(0, end .3) → b at 2.0 (≤ .3+2.5) → c at 4.5 (≤ 2.3+2.5): one cluster
    events = [
        ev(0.0, "audio", KIND_ENERGY_SPIKE, strength=1.0),
        ev(2.0, "visual", KIND_SCENE_CHANGE, strength=0.8),
        ev(4.5, "audio", KIND_TRANSIENT, strength=1.0),
    ]
    windows = fuse(events)
    assert len(windows) == 1
    assert len(windows[0].events) == 3


# ---- scoring mechanics ---------------------------------------------------------------

def test_score_is_deterministic_and_explained():
    events = [ev(10.0, "audio", KIND_ENERGY_SPIKE, strength=0.8),
              ev(10.1, "visual", KIND_MOTION_BURST, strength=0.4)]
    score, conf, reasons = score_cluster(events, FusionConfig())
    # weighted = (0.5·0.8 + 0.5·0.4) = 0.6 → 100·0.6·1.25 = 75
    assert score == 75.0
    assert conf == 1.0
    assert reasons["family_strengths"] == {"audio": 0.8, "visual": 0.4}
    assert reasons["weighted"] == 0.6
    assert set(reasons["event_kinds"]) == {KIND_ENERGY_SPIKE, KIND_MOTION_BURST}


def test_absent_family_is_not_counted_as_zero():
    # Visual-only group: weight_sum uses ONLY the present family →
    # weighted = strength (not strength/2).
    score, conf, reasons = score_cluster([ev(0.0, "visual", KIND_SCENE_CHANGE, strength=1.0)],
                                         FusionConfig())
    assert reasons["weighted"] == 1.0
    assert score == 50.0  # solo ×0.5
    assert conf == 0.5


def test_min_score_threshold_respected():
    cfg = FusionConfig(min_score=60.0)
    events = [ev(0.0, "audio", KIND_ENERGY_SPIKE, strength=0.5),
              ev(0.1, "visual", KIND_SCENE_CHANGE, strength=0.5)]
    # weighted 0.5 → 62.5 ≥ 60 → qualifies
    assert len(fuse(events, cfg)) == 1
    cfg_strict = FusionConfig(min_score=95.0)
    assert fuse(events, cfg_strict) == []  # 62.5 < 95


def test_config_validation():
    with pytest.raises(ValueError):
        FusionConfig(window_seconds=0)
    with pytest.raises(ValueError):
        FusionConfig(audio_weight=0)
    with pytest.raises(ValueError):
        FusionConfig(min_score=101)


def test_windows_sorted_by_start():
    events = [
        ev(200.0, "audio", KIND_ENERGY_SPIKE, strength=1.0),
        ev(100.0, "audio", KIND_ENERGY_SPIKE, strength=1.0),
        ev(150.0, "audio", KIND_ENERGY_SPIKE, strength=1.0),
    ]
    windows = fuse(events)
    assert [w.start for w in windows] == [100.0, 150.0, 200.0]
