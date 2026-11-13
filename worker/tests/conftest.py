"""Shared fixtures: FFmpeg/FFprobe binary resolution + synthetic media.

Binary resolution order (per binary):
  1. ``FFMPEG_BIN`` / ``FFPROBE_BIN`` environment override (CI, custom installs)
  2. the binary on PATH (GitHub runners ship ffmpeg/ffprobe)
  3. the npm static builds in the repo's node_modules (local dev; ``npm ci``
     installs ffmpeg-static / ffprobe-static)

Synthetic videos are generated on the fly with lavfi (testsrc + sine) — no
binary fixtures are committed to the repository.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_SYSTEM_DIRS = {"Linux": "linux", "Darwin": "darwin", "Windows": "win32"}
_MACHINE_DIRS = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}


def _node_static_binary(name: str) -> Path | None:
    if name == "ffmpeg":
        candidate = REPO_ROOT / "node_modules" / "ffmpeg-static" / "ffmpeg"
        return candidate if candidate.is_file() else None
    system_dir = _SYSTEM_DIRS.get(platform.system())
    machine_dir = _MACHINE_DIRS.get(platform.machine().lower())
    if not system_dir or not machine_dir:
        return None
    candidate = REPO_ROOT / "node_modules" / "ffprobe-static" / "bin" / system_dir / machine_dir / "ffprobe"
    return candidate if candidate.is_file() else None


def resolve_binary(name: str) -> str | None:
    env_override = os.environ.get(f"{name.upper()}_BIN")
    if env_override:
        return env_override
    on_path = shutil.which(name)
    if on_path:
        return on_path
    static = _node_static_binary(name)
    return str(static) if static else None


def _require_binary(name: str) -> str:
    path = resolve_binary(name)
    if not path:
        pytest.skip(f"{name} binary not available (set {name.upper()}_BIN or install ffmpeg)")
    return path


@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    return _require_binary("ffmpeg")


@pytest.fixture(scope="session")
def ffprobe_bin() -> str:
    return _require_binary("ffprobe")


def _run_ffmpeg(ffmpeg: str, args: list[str]) -> None:
    result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True)
    assert result.returncode == 0, f"ffmpeg failed: {result.stderr.decode('utf-8', 'replace')[:500]}"


@pytest.fixture(scope="session")
def synthetic_videos(ffmpeg_bin: str, tmp_path_factory) -> dict[str, Path]:
    """Generates the test media matrix once per session (small lavfi clips)."""
    out_dir = tmp_path_factory.mktemp("media")
    videos: dict[str, Path] = {}

    # Valid MP4: H.264 video + AAC audio, 2s, 320x240.
    valid = out_dir / "valid.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        [
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(valid),
        ],
    )
    videos["valid"] = valid

    # Video only (no audio track) — must still validate (audio is optional).
    video_only = out_dir / "video_only.mp4"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video_only)],
    )
    videos["video_only"] = video_only

    # Audio only (no video track) — must be rejected (no usable video stream).
    audio_only = out_dir / "audio_only.m4a"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "aac", str(audio_only)],
    )
    videos["audio_only"] = audio_only

    # Unsupported container (AVI) — must be rejected by the container whitelist.
    avi = out_dir / "unsupported.avi"
    _run_ffmpeg(
        ffmpeg_bin,
        ["-f", "lavfi", "-i", "testsrc=duration=1:size=128x96:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(avi)],
    )
    videos["unsupported"] = avi

    # Corrupt bytes with a video-looking extension — must be rejected.
    corrupt = out_dir / "corrupt.mp4"
    corrupt.write_bytes(b"this is definitely not an mp4 file " * 32)
    videos["corrupt"] = corrupt

    # Empty file — must be rejected before probing.
    empty = out_dir / "empty.mp4"
    empty.write_bytes(b"")
    videos["empty"] = empty

    return videos
