"""Deterministic synthetic audio fixtures for the intel tests (TASK 007).

Pure numpy + the stdlib ``wave`` module — no network, no paid services, fully
reproducible (fixed RNG seed). These stand in for real stream audio so the
detectors' POSITIVE and NEGATIVE cases are exact.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

RATE = 16000
_SEED = 20261113


def synth_segments(segments: list[tuple[float, float, str]], total_seconds: float) -> np.ndarray:
    """Builds a mono float32 signal in [-1, 1].

    ``segments`` = (start, duration, kind) with kind in:
      - "silence"    — digital zero
      - "quiet_tone" — 440 Hz sine at amplitude 0.01 (normal background speech level)
      - "loud_tone"  — 440 Hz sine at amplitude 0.4  (a shout-level level, NO bright edges)
      - "noise_burst"— seeded white noise at amplitude 0.4 (bright, transient-like)
      - "loud_noise" — seeded white noise at amplitude 0.4 sustained
    """
    n = int(total_seconds * RATE)
    buf = np.zeros(n, dtype=np.float32)
    rng = np.random.default_rng(_SEED)
    for start, duration, kind in segments:
        i0 = int(start * RATE)
        i1 = min(n, int((start + duration) * RATE))
        if i1 <= i0:
            continue
        m = i1 - i0
        t = np.arange(m, dtype=np.float32) / RATE
        if kind == "silence":
            pass
        elif kind == "quiet_tone":
            buf[i0:i1] = 0.01 * np.sin(2 * np.pi * 440 * t)
        elif kind == "loud_tone":
            buf[i0:i1] = 0.4 * np.sin(2 * np.pi * 440 * t)
        elif kind == "noise_burst":
            buf[i0:i1] = 0.4 * (2 * rng.random(m, dtype=np.float32) - 1)
        elif kind == "loud_noise":
            buf[i0:i1] = 0.4 * (2 * rng.random(m, dtype=np.float32) - 1)
        else:
            raise ValueError(f"unknown segment kind: {kind}")
    return buf


def write_wav(path: Path, samples: np.ndarray, rate: int = RATE) -> Path:
    """Writes mono 16-bit PCM WAV (the exact contract of extract_chunk_wav)."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())
    return path


# Stateful fake Supabase client — single implementation lives in
# app/intel/testing.py (shared with the benchmark runner). Re-exported here
# so existing test imports keep working.
from app.intel.testing import FakeIntelBackend, FakeSupabaseClient  # noqa: E402,F401
