"""Word-level animated captions (Phase 5B) — deterministic ASS generation.

Design constraints (see docs/ARCHITECTURE.md, "Captions (Phase 5B)"):

- The engine consumes ONLY real transcribed words (persisted by the analysis
  stage). It never fabricates, re-words or invents timestamps: invalid,
  out-of-range or empty entries are dropped, the rest are clamped and
  overlap-resolved deterministically.
- Output is libass-compatible ASS with per-word karaoke (``\\k``) timing.
  Verified libass semantics: a word switches from ``SecondaryColour``
  (upcoming / highlight) to ``PrimaryColour`` (spoken / base) at the end of
  its ``\\k`` window; the first word is already ``PrimaryColour`` at line
  start. The effect is the standard modern short-form progression: words
  glow in the highlight color and settle into the base color as they are
  spoken.
- Escaping: ASS markup characters in word text (``{`` ``}`` ``\\``) are
  escaped so transcribed text can never open an override block; control
  characters are removed. The ``ass=`` filter path is escaped for the
  FFmpeg filtergraph. The ASS file itself is written ONLY into the
  per-job temp directory (path contains a UUID + a fixed filename).
- RTL / Unicode: text is stored in LOGICAL order — libass (with
  libfribidi) performs the visual bidi reordering at render time; Arabic
  contextual shaping additionally requires libharfbuzz in the FFmpeg
  build (documented system requirement; deterministic fallback = the
  configured font's coverage via fontconfig).
- Pure functions only (no I/O except the single ``write_ass`` helper) so
  the engine is fully unit-testable without FFmpeg.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from .base import CaptionWord, CaptionStyle

#: Words closer than this to the previous word's end are overlap glitches;
#: the previous word wins the shared time (deterministic resolution).
_MIN_VISIBLE = 0.05  # seconds — anything shorter rounds to 0 in \\k centiseconds
_GAP_BREAK = 1.2  # seconds of silence that starts a new line
_LINE_TAIL = 0.10  # seconds the last word of a line stays on screen
_MAX_WORDS = 2000  # safety cap (180s * ~5 wps = ~900 is realistic)

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
_FILTER_SPECIALS = re.compile(r"(\\|:|,|;|\[|\]|=|')")


class CaptionsError(ValueError):
    """Captions could not be prepared (safe message, no payload internals)."""


# -- text hygiene ------------------------------------------------------------

def _sanitize_word(text: str) -> str:
    """Strips control characters and collapses whitespace; keeps Unicode as-is."""
    text = _CTRL_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _display_word(word: str, uppercase: bool) -> str:
    """Uppercases Latin words only (Arabic/CJK are never transformed)."""
    if uppercase and word.isascii():
        return word.upper()
    return word


# -- timing ------------------------------------------------------------------

def to_clip_relative(
    words: Sequence[CaptionWord],
    clip_start: float,
    duration: float,
) -> list[CaptionWord]:
    """Shifts absolute (audio-time) words into clip-relative time.

    Words are clamped to ``[0, duration]``; words with no remaining span
    after clamping are dropped (never fabricated).
    """
    if duration <= 0:
        return []
    out: list[CaptionWord] = []
    for w in words:
        if w.word.strip() == "" or not (math.isfinite(w.start) and math.isfinite(w.end)):
            continue
        s = max(0.0, min(w.start - clip_start, duration))
        e = max(0.0, min(w.end - clip_start, duration))
        if e <= s:
            continue
        out.append(CaptionWord(word=w.word, start=round(s, 3), end=round(e, 3)))
    return out


def clean_words(words: Sequence[CaptionWord], duration: float) -> list[CaptionWord]:
    """Validates, sorts and overlap-resolves clip-relative words.

    Deterministic rules:
    - sort by (start, original index) — stable for equal starts;
    - a word starting before the previous word's end is clipped to start
      at the previous word's end; if that removes its whole span it is
      dropped (the previous word wins the shared time);
    - spans shorter than ``_MIN_VISIBLE`` are extended rightwards up to the
      clip duration; if they still cannot be made visible they are dropped;
    - the result is capped at ``_MAX_WORDS`` (first words win).
    """
    if duration <= 0:
        return []
    ordered = sorted(enumerate(words), key=lambda pair: (pair[1].start, pair[0]))
    result: list[CaptionWord] = []
    prev_end = 0.0
    for _idx, w in ordered:
        text = _sanitize_word(w.word)
        if not text:
            continue
        s = min(max(w.start, 0.0), duration)
        e = min(max(w.end, 0.0), duration)
        if s < prev_end:
            s = prev_end
        if e <= s:
            continue
        if e - s < _MIN_VISIBLE:
            e = min(duration, s + _MIN_VISIBLE)
            if e <= s:
                continue
        result.append(CaptionWord(word=text, start=round(s, 3), end=round(e, 3)))
        prev_end = max(prev_end, e)
        if len(result) >= _MAX_WORDS:
            break
    return result


# -- line segmentation ---------------------------------------------------------

@dataclass(frozen=True)
class CaptionLine:
    """One on-screen line: a chunk of consecutive words with \\k timing."""

    start: float
    end: float
    words: tuple[CaptionWord, ...]
    karak: tuple[int, ...]  # per-word \\k duration in centiseconds (>= 1)


def _karak_centiseconds(start: float, end: float) -> int:
    return max(1, round((end - start) * 100))


def segment_lines(words: Sequence[CaptionWord], style: CaptionStyle) -> list[CaptionLine]:
    """Groups words into on-screen lines.

    A line breaks when the chunk reaches ``words_per_line`` words or when
    the gap to the next word exceeds ``_GAP_BREAK`` seconds (a natural
    phrase boundary). Line end = last word end + a small readability tail,
    and never shorter than the sum of the \\k windows.
    """
    lines: list[CaptionLine] = []
    chunk: list[CaptionWord] = []

    def flush() -> None:
        if not chunk:
            return
        karak = tuple(_karak_centiseconds(w.start, w.end) for w in chunk)
        start = chunk[0].start
        end = max(chunk[-1].end + _LINE_TAIL, start + sum(karak) / 100.0 + 0.02)
        lines.append(CaptionLine(start=round(start, 3), end=round(end, 3),
                                 words=tuple(chunk), karak=karak))
        chunk.clear()

    for w in words:
        if chunk and (len(chunk) >= style.words_per_line or w.start - chunk[-1].end > _GAP_BREAK):
            flush()
        chunk.append(w)
    flush()
    return lines


# -- ASS serialization ---------------------------------------------------------

def ass_color(rgb_hex: str) -> str:
    """Converts ``RRGGBB`` to the ASS ``&HAABBGGRR`` format (opaque)."""
    if not _COLOR_RE.match(rgb_hex):
        raise CaptionsError("caption color must be 6 hex digits (RRGGBB)")
    r, g, b = rgb_hex[0:2], rgb_hex[2:4], rgb_hex[4:6]
    return f"&H00{b.upper()}{g.upper()}{r.upper()}"


def format_ass_time(seconds: float) -> str:
    """ASS time format ``H:MM:SS.cc`` (centiseconds, clamped to >= 0)."""
    if seconds < 0:
        seconds = 0.0
    total_cs = int(round(seconds * 100))
    h, rem = divmod(total_cs, 3_600_00)
    m, rem = divmod(rem, 60_00)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    """Escapes ASS markup so text can never open an override block."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def filter_escape(value: str) -> str:
    """Escapes a value for an FFmpeg filtergraph option (e.g. ``ass=PATH``)."""
    return _FILTER_SPECIALS.sub(r"\\\1", value)


def build_ass(
    lines: Sequence[CaptionLine],
    style: CaptionStyle,
    play_res_x: int,
    play_res_y: int,
) -> str:
    """Serializes lines + style to an ASS document (deterministic bytes)."""
    style_line = (
        "Style: Cap,"
        f"{style.font_family},"
        f"{style.font_size},"
        f"{ass_color(style.base_color)},"
        f"{ass_color(style.highlight_color)},"
        f"{ass_color(style.outline_color)},"
        "&H80000000,"
        "-1,0,0,0,100,100,0,0,1,"
        f"{style.outline_width},0,2,40,40,{style.margin_v},1"
    )
    dialogues = []
    for line in lines:
        parts = []
        for word, karak in zip(line.words, line.karak):
            parts.append("{\\k%d}%s" % (karak, ass_escape(_display_word(word.word, style.uppercase))))
        dialogues.append(
            f"Dialogue: 0,{format_ass_time(line.start)},{format_ass_time(line.end)},"
            f"Cap,,0,0,0,,{' '.join(parts)}"
        )
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_res_x}\n"
        f"PlayResY: {play_res_y}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,"
        " BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
        " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style_line}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    return header + "\n".join(dialogues) + ("\n" if dialogues else "")


def build_captions_ass(
    words: Sequence[CaptionWord],
    style: Optional[CaptionStyle],
    duration: float,
    play_res_x: int = 1080,
    play_res_y: int = 1920,
) -> str | None:
    """Prepares clip-relative ``words`` into an ASS document.

    Returns ``None`` when no valid word survives cleaning (the caller then
    renders WITHOUT subtitles — the Phase 5A path — instead of failing).
    Raises :class:`CaptionsError` only for an invalid style configuration.
    """
    st = style or CaptionStyle()
    cleaned = clean_words(list(words), duration)
    if not cleaned:
        return None
    lines = segment_lines(cleaned, st)
    if not lines:
        return None
    return build_ass(lines, st, play_res_x, play_res_y)


def write_ass(text: str, path: Path) -> Path:
    """Writes the ASS document (UTF-8) into the per-job temp directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def parse_caption_payload(raw: Any, clip_start: float, duration: float) -> list[CaptionWord]:
    """Parses the persisted ``clips.captions`` jsonb payload.

    Expected shape: ``{"words": [{"w": str, "s": float, "e": float}, ...]}``
    with ABSOLUTE (audio-time) timestamps. Any malformed shape (wrong type,
    missing keys, junk numbers) degrades to "no captions for this clip" —
    the job must never crash on a bad row, and no word is ever invented.
    """
    if not isinstance(raw, dict):
        return []
    raw_words = raw.get("words")
    if not isinstance(raw_words, list):
        return []
    items: list[CaptionWord] = []
    for entry in raw_words:
        if isinstance(entry, dict):
            w, s, e = entry.get("w"), entry.get("s"), entry.get("e")
        elif isinstance(entry, CaptionWord):
            w, s, e = entry.word, entry.start, entry.end
        else:
            continue
        if not isinstance(w, str):
            continue
        try:
            s = float(s)
            e = float(e)
        except (TypeError, ValueError):
            continue
        items.append(CaptionWord(word=w, start=s, end=e))
    return to_clip_relative(items, clip_start, duration)
