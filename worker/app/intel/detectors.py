"""Signal detectors (TASK 007) — pure functions over measured features.

Each detector turns MEASURED feature data into ``SignalEvent``s on the
absolute timeline. No detector invents data: every emitted event carries
the window statistics it was computed from in its ``evidence`` dict.

Audio detectors (over the 20 Hz RMS/ZCR envelope):
- ``energy_spike``       — sudden rise vs the local ±5 s median baseline;
- ``transient``          — short (≤ 0.5 s), high-peak, high-ZCR burst
                           (explosion-like; game SFX family);
- ``voice_activity_change`` — sustained regime shift in RMS+ZCR
                           (HEURISTIC, labeled as such in evidence).

Visual detectors (over FFmpeg-measured cuts/motion):
- ``scene_change``       — measured cut (ffmpeg scene score > threshold);
- ``motion_burst``       — frame-difference surge vs local median.

Facecam / expression / HUD-OCR detectors are future adapters: while absent
their families simply contribute no evidence (the fusion then scores with
fewer families and lower confidence — clips never REQUIRE facecam).
"""

from __future__ import annotations

import numpy as np

from .audio_features import AudioChunkFeatures
from .signals import (
    KIND_ENERGY_SPIKE,
    KIND_MOTION_BURST,
    KIND_SCENE_CHANGE,
    KIND_TRANSIENT,
    KIND_VOICE_ACTIVITY_CHANGE,
    SignalEvent,
)
from .visual_features import VisualChunkFeatures

# -- audio thresholds (deterministic; tunable per deployment) -------------------
SPIKE_RATIO = 3.0          # rms must exceed k × local median
SPIKE_MIN_ABS = 0.02       # ...and be above ~ -34 dBFS (ignore quiet noise)
SPIKE_STRENGTH_SCALE = 2.0 # (ratio-1)/scale mapped to 0..1
TRANSIENT_MAX_SECONDS = 0.5
TRANSIENT_RATIO = 4.0
TRANSIENT_ZCR = 0.2        # bright (sibilant/impact-like), not a hum
REGIME_SHIFT = 0.4         # 40% relative change in the RMS mean
REGIME_MIN_SECONDS = 1.0   # ...sustained at least this long


def _local_baseline(rms: np.ndarray, i: int, half_window: int = 100) -> float:
    """Median RMS over ±5 s (100 samples at 20 Hz) EXCLUDING the sample itself."""
    lo = max(0, i - half_window)
    hi = min(len(rms), i + half_window)
    window = rms[lo:hi]
    if i - lo < len(window):
        window = np.concatenate([window[: i - lo], window[i - lo + 1 :]])
    if window.size == 0:
        return float(rms[i])
    return float(np.median(window))


def detect_audio_events(feats: AudioChunkFeatures) -> list[SignalEvent]:
    """Detects the audio signal kinds within one chunk (absolute times)."""
    rms = feats.rms.astype(np.float64)
    zcr = feats.zcr.astype(np.float64)
    hop = feats.hop_seconds
    events: list[SignalEvent] = []
    n = len(rms)
    if n == 0:
        return events

    # -- energy spikes -----------------------------------------------------
    i = 0
    while i < n:
        baseline = max(_local_baseline(rms, i), 1e-6)
        if rms[i] >= SPIKE_MIN_ABS and rms[i] >= SPIKE_RATIO * baseline:
            j = i
            peak = rms[i]
            while j + 1 < n and rms[j + 1] >= baseline * SPIKE_RATIO:
                j += 1
                peak = max(peak, rms[j])
            t0 = feats.offset + i * hop
            t1 = feats.offset + (j + 1) * hop
            strength = min(1.0, (peak / baseline - 1.0) / SPIKE_STRENGTH_SCALE)
            events.append(SignalEvent(
                t=round(t0, 3), t_end=round(min(t1, t0 + max(t1 - t0, hop)), 3),
                family="audio", kind=KIND_ENERGY_SPIKE, strength=round(float(strength), 4),
                evidence={
                    "family": "audio", "kind": KIND_ENERGY_SPIKE,
                    "strength": round(float(strength), 4),
                    "peak_rms": round(float(peak), 6),
                    "baseline_rms": round(baseline, 6),
                    "ratio": round(float(peak / baseline), 3),
                },
            ))
            i = j + 1
        else:
            i += 1

    # -- transients (short, bright, high-peak) --------------------------------
    max_burst = int(TRANSIENT_MAX_SECONDS / hop) + 1  # windows allowed in a burst
    i = 0
    while i < n:
        baseline = max(_local_baseline(rms, i), 1e-6)
        if rms[i] >= TRANSIENT_RATIO * baseline and zcr[i] >= TRANSIENT_ZCR:
            j = i
            while j + 1 < n and (j + 1 - i) < max_burst and rms[j + 1] >= TRANSIENT_RATIO * baseline:
                j += 1
            t0 = feats.offset + i * hop
            t1 = feats.offset + (j + 1) * hop
            strength = min(1.0, (float(rms[i:j + 1].max()) / baseline - 1.0) / (TRANSIENT_RATIO * 2.0))
            events.append(SignalEvent(
                t=round(t0, 3), t_end=round(t1, 3),
                family="audio", kind=KIND_TRANSIENT, strength=round(float(strength), 4),
                evidence={
                    "family": "audio", "kind": KIND_TRANSIENT,
                    "strength": round(float(strength), 4),
                    "peak_rms": round(float(rms[i:j + 1].max()), 6),
                    "zcr": round(float(zcr[i]), 4),
                    "duration_s": round(t1 - t0, 3),
                    "note": "short high-peak bright burst — may be game SFX, not a vocal reaction",
                },
            ))
            i = j + 1
        else:
            i += 1

    # -- voice-activity regime shift (HEURISTIC, labeled) ----------------------
    window = max(1, int(REGIME_MIN_SECONDS / hop))
    if n > 2 * window:
        for i in range(window, n - window):
            before = float(rms[i - window : i].mean())
            after = float(rms[i : i + window].mean())
            if before < 1e-4:
                continue
            change = (after - before) / before
            if abs(change) >= REGIME_SHIFT:
                z_before = float(zcr[i - window : i].mean())
                z_after = float(zcr[i : i + window].mean())
                t0 = feats.offset + (i - window) * hop
                t1 = feats.offset + (i + window) * hop
                events.append(SignalEvent(
                    t=round(t0, 3), t_end=round(t1, 3),
                    family="audio", kind=KIND_VOICE_ACTIVITY_CHANGE,
                    strength=round(min(1.0, abs(change) / 2.0), 4),
                    evidence={
                        "family": "audio", "kind": KIND_VOICE_ACTIVITY_CHANGE,
                        "strength": round(min(1.0, abs(change) / 2.0), 4),
                        "rms_change_ratio": round(float(change), 3),
                        "zcr_before": round(z_before, 4), "zcr_after": round(z_after, 4),
                        "heuristic": True,
                        "note": "sustained vocal-regime shift (heuristic — not a speech classification)",
                    },
                ))
                i += 2 * window  # one regime change per region
    return events


def detect_visual_events(feats: VisualChunkFeatures) -> list[SignalEvent]:
    """Detects the visual signal kinds within one chunk (absolute times)."""
    events: list[SignalEvent] = []
    for t in feats.scene_changes:
        events.append(SignalEvent(
            t=round(t, 3), t_end=round(t, 3),
            family="visual", kind=KIND_SCENE_CHANGE, strength=1.0,
            evidence={
                "family": "visual", "kind": KIND_SCENE_CHANGE, "strength": 1.0,
                "note": "measured cut (ffmpeg scene score above threshold)",
            },
        ))
    for t, s in feats.motion_peaks:
        events.append(SignalEvent(
            t=round(t, 3), t_end=round(t, 3),
            family="visual", kind=KIND_MOTION_BURST, strength=round(float(s), 4),
            evidence={
                "family": "visual", "kind": KIND_MOTION_BURST, "strength": round(float(s), 4),
                "note": "frame-difference surge (tblend/signalstats, 2 fps decimated)",
            },
        ))
    return events
