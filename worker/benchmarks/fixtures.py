"""Scripted synthetic fixture with ground-truth events (TASK 007 benchmark).

Layout (30 s, 320x240 @ 24 fps, mono 16 kHz audio):

    video:  testsrc [0, 8.5)  |  solid red [8.5, 25)  |  solid blue [25, 30)
    audio:  quiet 440 Hz bed (vol 0.05) throughout
            + white-noise burst 0.3 s @ 8.5  (vol 0.4)
            + white-noise burst 0.3 s @ 20.0 (vol 0.4)

Ground truth (core kinds — the signals the product cares about):

    E1  8.5   audio  energy_spike      ← burst, straddles chunk 0/1 boundary
    E2  8.5   audio  transient         ← burst, straddles chunk 0/1 boundary
    E3  8.5   visual scene_change      ← hard cut, straddles chunk 0/1 boundary
    E4  20.0  audio  energy_spike      ← audio-only burst (no facecam/visual)
    E5  20.0  audio  transient         ← audio-only burst
    E6  25.0  visual scene_change      ← cut, straddles chunk 2/3 boundary

With chunk=10 s / overlap=2 s the ownership bounds are
[0,9) [9,17) [17,25) [25,∞): E1–E3 are detected by BOTH neighbour chunks and
owned by chunk 0; E6 by both chunk 2 and chunk 3 and owned by chunk 3.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DURATION = 30.0
CUT_1 = 8.5
CUT_2 = 25.0
BURST_1 = 8.5
BURST_2 = 20.0
BURST_SECONDS = 0.3


@dataclass
class GroundTruthEvent:
    t: float
    family: str
    kind: str
    note: str = ""


@dataclass
class BenchmarkFixture:
    source: Path
    duration: float = DURATION
    events: list[GroundTruthEvent] = field(default_factory=list)
    straddled: list[GroundTruthEvent] = field(default_factory=list)


def build_stream_fixture(ffmpeg: str, out_dir: Path) -> BenchmarkFixture:
    """Generates the deterministic fixture (real FFmpeg lavfi, no network)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / "fixture-stream.mp4"
    if not src.is_file():
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={CUT_1}:size=320x240:rate=24",
            "-f", "lavfi", "-i", f"color=c=red:d={CUT_2 - CUT_1}:s=320x240:r=24",
            "-f", "lavfi", "-i", f"color=c=blue:d={DURATION - CUT_2}:s=320x240:r=24",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION}",
            "-f", "lavfi", "-i", f"anoisesrc=d={BURST_SECONDS}:color=white:amplitude=0.4",
            "-f", "lavfi", "-i", f"anoisesrc=d={BURST_SECONDS}:color=white:amplitude=0.4",
            "-filter_complex",
            f"[0:v][1:v][2:v]concat=n=3:v=1:a=0[vid];"
            f"[3:a]volume=0.05[a0];"
            f"[4:a]adelay={int(BURST_1 * 1000)}:all=1[a1];"
            f"[5:a]adelay={int(BURST_2 * 1000)}:all=1[a2];"
            f"[a0][a1][a2]amix=inputs=3:normalize=0[aout]",
            "-map", "[vid]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(src),
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(
                "fixture generation failed: " + result.stderr.decode("utf-8", "replace")[:300]
            )

    events = [
        GroundTruthEvent(BURST_1, "audio", "energy_spike", "loud burst (straddles chunk 0/1)"),
        GroundTruthEvent(BURST_1, "audio", "transient", "bright burst (straddles chunk 0/1)"),
        GroundTruthEvent(CUT_1, "visual", "scene_change", "hard cut (straddles chunk 0/1)"),
        GroundTruthEvent(BURST_2, "audio", "energy_spike", "audio-only burst, no facecam needed"),
        GroundTruthEvent(BURST_2, "audio", "transient", "audio-only bright burst"),
        GroundTruthEvent(CUT_2, "visual", "scene_change", "hard cut (straddles chunk 2/3)"),
    ]
    straddled = [e for e in events if "straddles" in e.note]
    return BenchmarkFixture(source=src, duration=DURATION, events=events, straddled=straddled)
