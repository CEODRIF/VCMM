"""Audio feature extraction + audio detector tests (TASK 007).

Feature math runs on deterministic synthetic PCM (intel_fixtures). The FFmpeg
extractor is tested against a REAL lavfi-generated file with bounded -ss/-t.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

from app.intel.audio_features import (
    SILENCE_RMS,
    AudioFeatureError,
    compute_audio_features,
    extract_chunk_wav,
    read_pcm16,
)
from app.intel.detectors import detect_audio_events
from intel_fixtures import synth_segments, write_wav


# ---- WAV I/O ------------------------------------------------------------------

def test_read_pcm16_round_trip(tmp_path):
    path = write_wav(tmp_path / "a.wav", np.full(1600, 0.5, dtype=np.float32))
    samples = read_pcm16(path)
    assert len(samples) == 1600
    assert np.allclose(samples, 0.5, atol=1e-4)


def test_read_pcm16_rejects_wrong_format(tmp_path):
    # stereo 16-bit — must be rejected (contract: mono 16 kHz s16le)
    import wave

    with wave.open(str(tmp_path / "stereo.wav"), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes((np.zeros(1600, dtype=np.int16)).tobytes())
    with pytest.raises(AudioFeatureError):
        read_pcm16(tmp_path / "stereo.wav")
    # empty file
    (tmp_path / "empty.wav").write_bytes(b"")
    with pytest.raises(AudioFeatureError):
        read_pcm16(tmp_path / "empty.wav")


# ---- feature math on deterministic synthetic PCM -------------------------------

def _feats(segments, total, offset=0.0):
    samples = synth_segments(segments, total)
    return compute_audio_features(samples, offset, total), samples


def test_silence_features_and_gaps():
    feats, _ = _feats([(0, 10, "silence")], 10.0, offset=1000.0)
    assert feats.rms.max() < SILENCE_RMS
    # The whole chunk is one silence gap, on the ABSOLUTE timeline.
    assert len(feats.silence_gaps) == 1
    start, end = feats.silence_gaps[0]
    assert start == pytest.approx(1000.0, abs=0.06)
    # The last envelope window ends slightly before t=10 s.
    assert 9.4 <= end - 1000.0 <= 10.0


def test_storage_summary_is_1hz_and_compact():
    feats, _ = _feats([(0, 10, "silence"), (5, 0.5, "noise_burst")], 10.0)
    summary = feats.storage_summary()
    assert summary["family"] == "audio"
    assert len(summary["rms_1hz"]) == 10      # one entry per source second
    assert len(summary["zcr_1hz"]) == 10
    assert len(summary["peak_per_second"]) == 10
    # 10 s of audio → a few dozen floats, not 320 KB of PCM: O(seconds), not O(samples).
    assert len(str(summary)) < 5000
    # The burst second is loud; the rest quiet.
    assert summary["peak_per_second"][5] >= 0.3
    assert summary["peak_per_second"][1] < 0.01
    assert summary["rms_1hz"][5] > summary["rms_1hz"][1]


def test_absolute_timestamps_use_chunk_offset():
    feats, _ = _feats([(0, 10, "quiet_tone"), (6, 0.5, "noise_burst")], 10.0, offset=1200.0)
    events = detect_audio_events(feats)
    assert events, "burst on a quiet background must produce events"
    for e in events:
        assert e.t >= 1199.9  # absolute, not relative
    spikes = [e for e in events if e.kind == "energy_spike"]
    # Detector resolution is one 250 ms window (event t = window start).
    assert spikes and spikes[0].t == pytest.approx(1206.0, abs=0.3)


def test_loudness_alone_produces_no_events():
    # A uniformly loud tone is NOT a reaction: no local change → nothing fires.
    feats, _ = _feats([(0, 10, "loud_tone")], 10.0)
    assert detect_audio_events(feats) == []


def test_energy_spike_positive_and_negative():
    # positive: 3x+ rise above a real (nonzero) baseline
    feats, _ = _feats([(0, 10, "quiet_tone"), (5, 0.8, "loud_tone")], 10.0)
    events = detect_audio_events(feats)
    kinds = {e.kind for e in events}
    assert "energy_spike" in kinds
    spike = next(e for e in events if e.kind == "energy_spike")
    assert spike.t == pytest.approx(5.0, abs=0.3)  # 250 ms window resolution
    assert spike.strength > 0
    # evidence is MEASURED, not fabricated
    assert spike.evidence["peak_rms"] > spike.evidence["baseline_rms"] > 0
    assert spike.evidence["ratio"] >= 3.0
    # negative: constant background → no spike
    feats_q, _ = _feats([(0, 10, "quiet_tone")], 10.0)
    assert not any(e.kind == "energy_spike" for e in detect_audio_events(feats_q))


def test_transient_positive_and_negative():
    # positive: short (0.3 s) bright burst
    feats, _ = _feats([(0, 10, "quiet_tone"), (5, 0.3, "noise_burst")], 10.0)
    transients = [e for e in detect_audio_events(feats) if e.kind == "transient"]
    assert transients, "bright burst must be flagged as a transient"
    tr = transients[0]
    assert tr.t == pytest.approx(5.0, abs=0.3)  # 250 ms window resolution
    assert tr.evidence["duration_s"] <= 0.6
    assert tr.evidence["zcr"] >= 0.2
    # negative: a SUSTAINED loud tone is not a transient (not short & bright)
    feats_tone, _ = _feats([(0, 10, "quiet_tone"), (5, 2.0, "loud_tone")], 10.0)
    assert not any(e.kind == "transient" for e in detect_audio_events(feats_tone))


def test_voice_activity_change_heuristic():
    # sustained regime shift: quiet background → 2 s of louder steady tone
    feats, _ = _feats([(0, 5, "quiet_tone"), (5, 2, "loud_tone"), (7, 3, "quiet_tone")], 10.0)
    events = detect_audio_events(feats)
    regime = [e for e in events if e.kind == "voice_activity_change"]
    assert regime, "a sustained 4x+ level shift must be flagged (heuristic)"
    ev = regime[0]
    assert ev.evidence["heuristic"] is True
    assert ev.t <= 5.5
    # and a flat signal never fires it
    feats_flat, _ = _feats([(0, 10, "quiet_tone")], 10.0)
    assert not any(e.kind == "voice_activity_change" for e in detect_audio_events(feats_flat))


def test_silence_gap_detection_with_tone():
    # tone → silence → tone: the silence middle becomes a snap point
    feats, _ = _feats([(0, 4, "loud_tone"), (4, 2, "silence"), (6, 4, "loud_tone")], 10.0, offset=200.0)
    assert feats.silence_gaps, "2 s of silence must be detected"
    start, end = feats.silence_gaps[0]
    assert start == pytest.approx(204.0, abs=0.1)
    assert end == pytest.approx(205.8, abs=0.15)  # 20 Hz envelope resolution


def test_empty_pcm_produces_no_events():
    feats = compute_audio_features(np.zeros(0, dtype=np.float32), 0.0, 0.0)
    assert detect_audio_events(feats) == []


# ---- bounded FFmpeg extraction (real binary) -----------------------------------

def test_extract_chunk_wav_bounded_range(ffmpeg_bin, render_media, tmp_path):
    # 6 s source, ask for [2, 5): the decode is bounded and the result ~3 s.
    destination = tmp_path / "chunk.wav"
    asyncio.run(extract_chunk_wav(ffmpeg_bin, render_media["landscape"], 2.0, 3.0,
                                  destination, 60.0))
    assert destination.is_file()
    samples = read_pcm16(destination)
    assert 16000 * 2.7 <= len(samples) <= 16000 * 3.3


def test_extract_chunk_wav_missing_source(tmp_path):
    with pytest.raises(AudioFeatureError):
        asyncio.run(extract_chunk_wav("ffmpeg", tmp_path / "nope.mp4", 0.0, 1.0,
                                      tmp_path / "out.wav", 10.0))


def test_extract_chunk_wav_timeout_kills_and_cleans(ffmpeg_bin, render_media, tmp_path):
    destination = tmp_path / "chunk.wav"
    with pytest.raises(AudioFeatureError):
        # 10 ms: even spawning the subprocess takes longer — the timeout
        # MUST fire and clean up (no race with a fast decode).
        asyncio.run(extract_chunk_wav(ffmpeg_bin, render_media["landscape"], 0.0, 3.0,
                                      destination, 0.01))
    assert not destination.exists()  # temp file removed on the failure path
