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


def patch_mp4_rotation(path: Path, degrees: int = 90) -> None:
    """Injects a ``rot `` box (value = degrees/16) into the video track of an
    MP4 — the same atom phones write for rotated captures.

    ffmpeg >= 5 no longer honours ``-metadata rotate=`` on (re)mux, and
    older ffprobe (4.x) does not expose side data in JSON, so the rotation
    tag is written directly. Safe only when ``moov`` is the LAST top-level
    box (no ``-movflags +faststart``): the 12-byte insertion grows the box
    sizes without shifting ``mdat`` (so ``stco`` offsets stay valid).
    """
    import struct

    data = bytearray(path.read_bytes())

    def boxes(buf, start, end):
        pos = start
        while pos + 8 <= end:
            size = int.from_bytes(buf[pos:pos + 4], "big")
            box_type = bytes(buf[pos + 4:pos + 8])
            if size < 8:
                return
            yield pos, size, box_type
            pos += size

    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts"}

    def contains(buf, start, end, name):
        for pos, size, box_type in boxes(buf, start, end):
            if box_type == name:
                return True
            if box_type in containers and size > 8 and contains(buf, pos + 8, pos + size, name):
                return True
        return False

    top = list(boxes(data, 0, len(data)))
    if not any(t == b"ftyp" for _p, _s, t in top):
        raise ValueError("not an MP4 file")
    moovs = [(p, s) for p, s, t in top if t == b"moov"]
    if not moovs:
        raise ValueError("no moov box")
    moov_pos, moov_size = moovs[0]
    last_end = max((p + s) for p, s, _t in top)
    if moov_pos + moov_size < last_end:
        raise ValueError("moov is not the last box (faststart file); refusing to patch")

    target = None
    for trak_pos, trak_size, _t in boxes(data, moov_pos + 8, moov_pos + moov_size):
        if not contains(data, trak_pos + 8, trak_pos + trak_size, b"vmhd"):
            continue
        mdias = [(p, s) for p, s, t in boxes(data, trak_pos + 8, trak_pos + trak_size) if t == b"mdia"]
        assert mdias, "video track without mdia"
        target = (moov_pos, trak_pos, mdias[0][0])
        break
    if target is None:
        raise ValueError("no video track")

    moov_pos, trak_pos, mdia_pos = target
    atom = struct.pack(">I4sI", 12, b"rot ", degrees * 16)
    insert_at = mdia_pos + 8
    data[insert_at:insert_at] = atom
    for pos in (moov_pos, trak_pos, mdia_pos):
        struct.pack_into(">I", data, pos, int.from_bytes(data[pos:pos + 4], "big") + len(atom))
    path.write_bytes(bytes(data))


@pytest.fixture(scope="session")
def render_media(ffmpeg_bin: str, tmp_path_factory) -> dict[str, Path]:
    """Generates the Phase 5 orientation matrix once per session.

    - ``landscape``: 1920x1080 @30, 6s, H.264+AAC
    - ``portrait``: 720x1280 @30, 6s, H.264+AAC
    - ``square``: 640x640 @30, 6s, H.264+AAC
    - ``rotated``: the landscape clip with a ``rot `` box (90°) injected —
      the file stores 1920x1080 frames but DISPLAYS as 1080x1920 portrait
      (the phone-camera case).
    """
    out_dir = tmp_path_factory.mktemp("render-media")
    media: dict[str, Path] = {}

    def make(name: str, size: str) -> None:
        _run_ffmpeg(
            ffmpeg_bin,
            [
                "-f", "lavfi", "-i", f"testsrc=duration=6:size={size}:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest",
                str(out_dir / name),
            ],
        )

    make("landscape.mp4", "1920x1080")
    media["landscape"] = out_dir / "landscape.mp4"
    make("portrait.mp4", "720x1280")
    media["portrait"] = out_dir / "portrait.mp4"
    make("square.mp4", "640x640")
    media["square"] = out_dir / "square.mp4"

    rotated = out_dir / "rotated.mp4"
    rotated.write_bytes(media["landscape"].read_bytes())
    patch_mp4_rotation(rotated, 90)
    media["rotated"] = rotated

    return media
