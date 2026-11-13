"""Bounded audio feature extraction (TASK 007).

Extracts ONE chunk's audio from the already-verified source file with a
bounded ``-ss/-t`` decode (mono 16 kHz PCM s16le on disk) and reduces it
to compact, deterministic signal features with numpy:

- windowed RMS envelope (vocal/overall energy) at 20 Hz,
- zero-crossing rate (brightness — screams/hits read higher than speech),
- per-second peaks,
- silence gaps (used later to snap clip boundaries so speech is not cut),

and a **storage summary** (1 Hz downsample + gaps) that is what gets
checkpointed. Raw PCM is held only for the duration of one chunk's
processing (bounded: chunk_seconds × 16000 × 2 bytes ≈ 19 MB at 600 s)
and deleted afterwards — a long recording is never fully in RAM.
"""

from __future__ import annotations

import asyncio
import logging
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("clipforge.intel.audio")

#: Feature resolution in memory (20 Hz) and storage (1 Hz).
HOP_MS = 50
WINDOW_MS = 250
SAMPLE_RATE = 16000
HOP = int(HOP_MS / 1000 * SAMPLE_RATE)      # 800 samples (0.05 s)
WINDOW = int(WINDOW_MS / 1000 * SAMPLE_RATE)  # 4000 samples (0.25 s)

#: A silence gap is at least this long (s) to be a useful boundary snap point.
MIN_SILENCE_GAP = 0.35
SILENCE_RMS = 0.003  # ~ -50 dBFS; below this, the window is treated as silent


class AudioFeatureError(RuntimeError):
    """The chunk's audio could not be decoded/analyzed (safe, no internals)."""


@dataclass
class AudioChunkFeatures:
    """In-memory features for one chunk (absolute timeline)."""

    offset: float
    duration: float
    rms: np.ndarray       # 20 Hz windowed RMS (float32)
    zcr: np.ndarray       # 20 Hz zero-crossing rate (0..1)
    peak_per_second: list[float]
    silence_gaps: list[tuple[float, float]]  # ABSOLUTE (start, end) seconds

    @property
    def hop_seconds(self) -> float:
        return HOP_MS / 1000.0  # 0.05 s per envelope sample (RELATIVE to offset)

    def to_absolute(self, times: np.ndarray | list[float]) -> np.ndarray:
        return np.asarray(times, dtype=np.float64) + self.offset

    def storage_summary(self) -> dict[str, Any]:
        """Compact 1 Hz summary — the ONLY thing persisted per chunk."""
        rms = self.rms.astype(np.float64)
        n_seconds = max(1, int(np.ceil(self.duration)))
        summary_rms: list[float] = []
        summary_zcr: list[float] = []
        for s in range(n_seconds):
            sel = slice(s * (1000 // HOP_MS), (s + 1) * (1000 // HOP_MS))
            if rms[sel].size:
                summary_rms.append(round(float(rms[sel].mean()), 6))
                summary_zcr.append(round(float(self.zcr[sel].mean()), 6))
            else:
                summary_rms.append(0.0)
                summary_zcr.append(0.0)
        return {
            "family": "audio",
            "offset": round(self.offset, 3),
            "duration": round(self.duration, 3),
            "rms_1hz": summary_rms,
            "zcr_1hz": summary_zcr,
            "silence_gaps": [[round(a, 3), round(b, 3)] for a, b in self.silence_gaps],
            "peak_per_second": [round(p, 6) for p in self.peak_per_second],
        }


def read_pcm16(wav_path: Path) -> np.ndarray:
    """Reads a mono 16-bit PCM WAV into a float32 array in [-1, 1].

    The file is at most one chunk (bounded by construction); reading it is
    the ONLY whole-chunk memory allocation in the pipeline.
    """
    try:
        with wave.open(str(wav_path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() != SAMPLE_RATE:
                raise AudioFeatureError("chunk wav is not mono 16-bit PCM")
            raw = wf.readframes(wf.getnframes())
    except (EOFError, OSError) as exc:  # truncated / corrupt wav header
        raise AudioFeatureError("chunk wav could not be read") from exc
    if not raw:
        raise AudioFeatureError("chunk wav contains no samples")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return samples


def _windows(samples: np.ndarray) -> np.ndarray:
    """Non-overlapping-step view of (WINDOW, n_windows) at HOP spacing."""
    step = max(1, HOP)
    n = (len(samples) - WINDOW) // step + 1
    if n <= 0:
        padded = np.zeros(WINDOW, dtype=np.float32)
        padded[: len(samples)] = samples
        return padded.reshape(1, -1)
    base = samples[: n * step + (WINDOW - step)]
    view = np.lib.stride_tricks.sliding_window_view(base, WINDOW)[::step]
    return view


def compute_audio_features(samples: np.ndarray, offset: float, duration: float) -> AudioChunkFeatures:
    """Computes the full in-memory feature set for one chunk."""
    windows = _windows(samples)
    rms = np.sqrt((windows.astype(np.float64) ** 2).mean(axis=1)).astype(np.float32)
    signs = np.sign(windows)
    crossings = (np.abs(np.diff(signs, axis=1)) > 0).sum(axis=1)
    zcr = (crossings / np.maximum(1, windows.shape[1] - 1)).astype(np.float32)

    # Per-second peaks (absolute-friendly: stored relative, absolute via offset).
    samples_per_sec = SAMPLE_RATE
    n_seconds = max(1, int(np.ceil(duration)))
    peak_per_second: list[float] = []
    for s in range(n_seconds):
        sel = samples[s * samples_per_sec : (s + 1) * samples_per_sec]
        peak_per_second.append(float(np.max(np.abs(sel))) if sel.size else 0.0)

    # Silence gaps (absolute timeline).
    silent = rms < SILENCE_RMS
    gaps: list[tuple[float, float]] = []
    i = 0
    n = len(silent)
    while i < n:
        if silent[i]:
            j = i
            while j < n and silent[j]:
                j += 1
            start_rel = i * (HOP_MS / 1000.0)
            end_rel = j * (HOP_MS / 1000.0)
            if end_rel - start_rel >= MIN_SILENCE_GAP:
                gaps.append((offset + start_rel, offset + end_rel))
            i = j
        else:
            i += 1

    return AudioChunkFeatures(
        offset=offset,
        duration=duration,
        rms=rms,
        zcr=zcr,
        peak_per_second=peak_per_second,
        silence_gaps=gaps,
    )


async def extract_chunk_wav(
    ffmpeg_path: str,
    source: Path,
    offset: float,
    length: float,
    destination: Path,
    timeout_seconds: float,
) -> Path:
    """Decodes ONE bounded audio range of ``source`` to mono 16 kHz WAV.

    Streaming ``-ss/-t`` decode: FFmpeg never holds the whole file, the
    output is bounded (``length`` seconds), and the destination is removed
    on every failure path.
    """
    if not source.is_file() or source.stat().st_size == 0:
        raise AudioFeatureError("source file is missing or empty")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)

    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{offset:.3f}", "-i", str(source),
        "-t", f"{length:.3f}",
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
        str(destination),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        destination.unlink(missing_ok=True)
        raise AudioFeatureError("chunk audio decode timed out") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        destination.unlink(missing_ok=True)
        detail = (stderr or b"").decode("utf-8", "replace")[:200].strip()
        # Log-safe: never include the path, only the error class.
        logger.warning("chunk audio decode failed rc=%s (%s)", proc.returncode, detail[:80])
        raise AudioFeatureError("chunk audio could not be decoded")
    if not destination.is_file() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise AudioFeatureError("chunk audio decode produced no data")
    return destination
