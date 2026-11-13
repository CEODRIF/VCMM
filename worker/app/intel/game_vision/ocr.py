"""OCR abstraction + local Tesseract engine (TASK 009).

Policy (matches the facecam model policy, TASK 008):
- LOCAL inference only. No cloud OCR, no paid API, no external AI call.
- The tesseract BINARY and its traineddata come from an explicitly
  configured system installation (apt ``tesseract-ocr``); they are
  VALIDATED deterministically (binary -> tessdata -> version probe) and
  are NEVER downloaded during job execution.
- Missing binary / missing tessdata / OCR failure => honest
  ``unavailable`` (recorded, no fabricated text) — never an exception
  that breaks the pipeline.

The engine shells out to the tesseract CLI (TSV output) instead of using
a Python wrapper, so it inherits the repository's exact subprocess
discipline: bounded args, wall-clock timeout with process KILL, exit
code checked, stderr kept out of logs.

Text handling is deliberately CONSERVATIVE:
- stored text = strip + collapse inner whitespace (case & punctuation
  preserved — nothing "fixed" that tesseract did not read);
- the *dedup key* (used to match a line across frames) is more
  aggressive (case-folded, punctuation-free) — documented, not hidden.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import GameVisionConfig

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


class OcrError(RuntimeError):
    """An OCR operation failed (safe message — no internal paths)."""


def normalize_text(text: str) -> str:
    """Conservative normalization: strip + collapse whitespace only."""
    return _WS.sub(" ", text).strip()


def dedupe_key(text: str) -> str:
    """Cross-frame matching key: case-folded, punctuation removed.

    Used ONLY to decide "same line as last frame" — stored evidence keeps
    the conservative ``normalize_text`` form.
    """
    return _WS.sub(" ", _PUNCT.sub("", text)).strip().casefold()


@dataclass(frozen=True)
class OcrLine:
    """One measured text line in an image (evidence, not interpretation)."""

    text: str                    # conservative normalization
    confidence: float            # 0..100 (mean of word confidences)
    bbox: tuple[int, int, int, int]  # x, y, w, h in image pixels
    key: str = ""                # dedupe_key(text); filled by the engine


class OcrEngine:
    """Interface. ``available`` is checked before any chunk work."""

    name: str = "base"
    version: str | None = None

    def __init__(self) -> None:
        self.available = False
        self.unavailable_reason: str | None = None

    async def validate(self) -> None:  # pragma: no cover - interface
        """Deterministic validation; raises OcrError on failure."""
        raise NotImplementedError

    async def recognize(
        self, image_path: Path, work_dir: Path
    ) -> list[OcrLine]:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - interface
        return None


class NullOcrEngine(OcrEngine):
    """Honest fallback: game vision disabled/unconfigured."""

    name = "null"

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.available = False
        self.unavailable_reason = reason

    async def validate(self) -> None:
        raise OcrError(self.unavailable_reason or "ocr unavailable")


class TesseractOcrEngine(OcrEngine):
    """Local tesseract CLI engine (CPU, bounded subprocess per call)."""

    name = "tesseract"

    def __init__(self, config: GameVisionConfig) -> None:
        super().__init__()
        self._cfg = config
        self._binary: str | None = None
        self._ready = False

    async def validate(self) -> None:
        """binary -> tessdata -> version probe. Deterministic order so the
        reported reason is the same in every environment."""
        if self._ready:
            return
        cfg = self._cfg

        if cfg.tesseract_binary:
            if not Path(cfg.tesseract_binary).is_file():
                raise OcrError(
                    "configured tesseract binary is not a readable file "
                    f"(WORKER_GAME_VISION_TESSERACT_BINARY={Path(cfg.tesseract_binary).name})"
                )
            binary = cfg.tesseract_binary
        else:
            binary = shutil.which("tesseract")
            if not binary:
                raise OcrError(
                    "tesseract binary not found on PATH (install the system "
                    "tesseract-ocr package, or set "
                    "WORKER_GAME_VISION_TESSERACT_BINARY)"
                )

        if cfg.tessdata_dir:
            trained = Path(cfg.tessdata_dir) / f"{cfg.ocr_language}.traineddata"
            if not trained.is_file():
                raise OcrError(
                    f"tessdata not found: {trained.name} in the configured "
                    "WORKER_GAME_VISION_TESSDATA_DIR (the language pack must "
                    "be installed explicitly — never downloaded at runtime)"
                )

        args = [binary, "--version"]
        if cfg.tessdata_dir:
            args += ["--tessdata-dir", cfg.tessdata_dir]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise OcrError("tesseract --version timed out") from None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode != 0:
            raise OcrError("tesseract version probe failed (corrupt install?)")

        first = out.decode("utf-8", "replace").splitlines()
        self._binary = binary
        self.version = first[0] if first else "unknown"
        self._ready = True
        self.available = True
        self.unavailable_reason = None

    async def recognize(
        self, image_path: Path, work_dir: Path
    ) -> list[OcrLine]:
        """Runs ONE bounded tesseract call; returns measured lines (may be
        an empty list — 'no text' is a result, not an error)."""
        if not self._ready:
            raise OcrError("engine not validated")
        if not image_path.is_file():
            raise OcrError("ocr input image missing")

        base = work_dir / "ocr_out"
        args = [self._binary, str(image_path), str(base), "-l", self._cfg.ocr_language, "tsv"]
        if self._cfg.tessdata_dir:
            args += ["--tessdata-dir", self._cfg.tessdata_dir]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._cfg.ocr_timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise OcrError("ocr timed out (killed)") from None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        tsv = base.with_suffix(".tsv")
        if proc.returncode != 0 or not tsv.is_file():
            raise OcrError(f"ocr failed (exit {proc.returncode})")

        lines = _parse_tsv(tsv, self._cfg.min_ocr_confidence)
        tsv.unlink(missing_ok=True)
        return lines

    async def close(self) -> None:
        # No persistent subprocess — nothing to release.
        self._ready = False


def _parse_tsv(tsv_path: Path, min_confidence: float) -> list[OcrLine]:
    """Parses tesseract TSV into measured lines (grouped by line_num,
    ordered top-to-bottom). Mean word confidence; union bbox."""
    rows: list[tuple[int, int, int, int, int, int, int, int, float, str]] = []
    try:
        with tsv_path.open("r", encoding="utf-8", errors="replace") as fh:
            header = fh.readline().rstrip("\n").split("\t")
            idx = {name: i for i, name in enumerate(header)}
            for raw in fh:
                cols = raw.rstrip("\n").split("\t")
                if len(cols) < len(header):
                    continue
                text = cols[idx["text"]].strip()
                conf = float(cols[idx["conf"]])
                if not text or conf < 0:  # -1 = tesseract rejected word
                    continue
                rows.append((
                    int(cols[idx["block_num"]]), int(cols[idx["par_num"]]),
                    int(cols[idx["line_num"]]), int(cols[idx["word_num"]]),
                    int(cols[idx["left"]]), int(cols[idx["top"]]),
                    int(cols[idx["width"]]), int(cols[idx["height"]]),
                    conf, text,
                ))
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise OcrError(f"ocr output unreadable: {exc.__class__.__name__}") from exc

    grouped: dict[tuple[int, int, int], list[tuple[int, int, int, int, float, str]]] = {}
    for block, par, line, _word, left, top, width, height, conf, text in rows:
        grouped.setdefault((block, par, line), []).append((left, top, width, height, conf, text))

    out: list[OcrLine] = []
    for (block, par, line), words in grouped.items():
        words.sort(key=lambda w: w[0])  # left-to-right
        text = " ".join(normalize_text(w[5]) for w in words if normalize_text(w[5]))
        text = normalize_text(text)
        if not text:
            continue
        confs = [w[4] for w in words]
        confidence = round(sum(confs) / len(confs), 2)
        if confidence < min_confidence:
            continue
        x0 = min(w[0] for w in words)
        y0 = min(w[1] for w in words)
        x1 = max(w[0] + w[2] for w in words)
        y1 = max(w[1] + w[3] for w in words)
        out.append(OcrLine(text=text, confidence=confidence,
                           bbox=(x0, y0, x1 - x0, y1 - y0),
                           key=dedupe_key(text)))
    out.sort(key=lambda l: (l.bbox[1], l.bbox[0]))
    return out


def make_stub_engine(lines_by_frame: dict[int, list[OcrLine]],
                     name: str = "stub") -> "StubOcrEngine":
    """Test double: deterministic per-frame-index text (see test file)."""
    return StubOcrEngine(lines_by_frame, name)


class StubOcrEngine(OcrEngine):
    """Deterministic OCR double for tests (no subprocess, no native dep)."""

    def __init__(self, lines_by_frame: dict[int, list[OcrLine]],
                 name: str = "stub", fail_frames: Optional[set[int]] = None,
                 min_confidence: float = 0.0) -> None:
        super().__init__()
        self.name = name
        self.version = "test-stub-1.0"
        self._lines = lines_by_frame
        self._fail = fail_frames or set()
        self._min_conf = min_confidence
        self._delay: float = 0.0  # tests set this to simulate OCR cost
        self.calls: list[int] = []
        self.available = True
        self.unavailable_reason = None

    async def validate(self) -> None:
        return None

    async def recognize(self, image_path: Path, work_dir: Path) -> list[OcrLine]:
        index = int(image_path.stem.split("_")[1]) - 1  # f_%05d, 1-based
        self.calls.append(index)
        if self._delay:
            await asyncio.sleep(self._delay)
        if index in self._fail:
            raise OcrError("stub: simulated ocr failure")
        lines = self._lines.get(index, [])
        return [l for l in lines if l.confidence >= self._min_conf]
