"""Captions engine unit tests (Phase 5B) — pure, no FFmpeg needed.

Covers: text hygiene (Unicode/RTL/control chars), clip-relative conversion,
invalid/overlapping/out-of-range timestamp handling (never fabricated),
deterministic line segmentation, ASS serialization (colors, times, \\k
timing), escaping (ASS markup + filtergraph paths), style validation and
the persisted-payload parser.
"""

from __future__ import annotations

import math

import pytest

from app.rendering.base import CaptionStyle, CaptionWord
from app.rendering.captions import (
    _display_word,
    _sanitize_word,
    ass_color,
    ass_escape,
    build_ass,
    build_captions_ass,
    CaptionsError,
    clean_words,
    filter_escape,
    format_ass_time,
    parse_caption_payload,
    segment_lines,
    to_clip_relative,
)

STYLE = CaptionStyle()


def words(*specs: tuple[str, float, float]) -> list[CaptionWord]:
    return [CaptionWord(word=w, start=s, end=e) for w, s, e in specs]


# -- text hygiene ---------------------------------------------------------------

def test_sanitize_strips_control_chars_and_collapses_whitespace():
    assert _sanitize_word("a\x00b\x1fc") == "a b c"  # control chars -> space, collapsed
    assert _sanitize_word("  hello \t\t world\n") == "hello world"
    assert _sanitize_word("   ") == ""


def test_sanitize_preserves_unicode_and_rtl_as_is():
    arabic = "مرحبا بالعالم"
    german = "Grüße Straße"
    assert _sanitize_word(arabic) == arabic  # logical order kept; libass renders bidi
    assert _sanitize_word(german) == german
    assert _sanitize_word("mix ed EN العربية ok") == "mix ed EN العربية ok"


def test_display_uppercase_latin_only():
    assert _display_word("hello", True) == "HELLO"
    assert _display_word("مرحبا", True) == "مرحبا"  # never transformed
    assert _display_word("hello", False) == "hello"
    assert _display_word("café", True) == "café"  # non-ascii words untouched


# -- clip-relative conversion -----------------------------------------------------

def test_to_clip_relative_shifts_and_clamps():
    out = to_clip_relative(
        words(("a", 5.0, 6.0), ("b", 10.0, 12.0), ("c", 14.0, 16.0)),
        clip_start=10.0,
        duration=10.0,
    )
    # a: outside left -> dropped; b: inside; c: clamped to the window end
    assert [(w.word, w.start, w.end) for w in out] == [("b", 0.0, 2.0), ("c", 4.0, 6.0)]


def test_to_clip_relative_drops_empty_and_non_finite():
    out = to_clip_relative(
        words(("  ", 1.0, 2.0), ("a", math.inf, 2.0), ("b", 1.0, math.nan),
              ("c", float("inf"), float("inf")), ("ok", 1.0, 2.0)),
        clip_start=0.0,
        duration=10.0,
    )
    assert [w.word for w in out] == ["ok"]


def test_to_clip_relative_zero_duration_is_empty():
    assert to_clip_relative(words(("a", 0.0, 1.0)), 0.0, 0.0) == []
    assert to_clip_relative(words(("a", 0.0, 1.0)), 0.0, -5.0) == []


# -- clean_words ------------------------------------------------------------------

def test_clean_drops_invalid_keeps_valid_sorted():
    out = clean_words(
        words(("b", 2.0, 3.0), ("a", 5.0, 6.0), ("", 0.0, 1.0),
              ("x", 3.0, 3.0), ("y", 4.0, 2.0), ("c", 1.0, 2.0)),
        duration=10.0,
    )
    assert [(w.word, w.start, w.end) for w in out] == [
        ("c", 1.0, 2.0), ("b", 2.0, 3.0), ("a", 5.0, 6.0),
    ]


def test_clean_resolves_overlaps_deterministically():
    # "next" starts inside "prev": it is clipped to prev's end.
    out = clean_words(words(("prev", 1.0, 3.0), ("next", 2.0, 4.0)), duration=10.0)
    assert [(w.word, w.start, w.end) for w in out] == [
        ("prev", 1.0, 3.0), ("next", 3.0, 4.0),
    ]


def test_clean_drops_fully_swallowed_overlap():
    out = clean_words(words(("big", 1.0, 5.0), ("inside", 2.0, 4.0)), duration=10.0)
    assert [w.word for w in out] == ["big"]


def test_clean_extends_sub_minimal_spans():
    out = clean_words(words(("tick", 1.0, 1.01)), duration=10.0)
    assert out[0].word == "tick"
    assert out[0].end - out[0].start >= 0.05


def test_clean_drops_zero_span_at_window_end():
    out = clean_words(words(("edge", 10.0, 10.0)), duration=10.0)
    assert out == []
    # a 1-frame flash right at the end is still legal (k rounded to >= 1cs)
    kept = clean_words(words(("edge", 9.99, 10.0)), duration=10.0)
    assert [(w.word, w.start, w.end) for w in kept] == [("edge", 9.99, 10.0)]


def test_clean_clamps_to_window_bounds():
    out = clean_words(words(("a", -5.0, 1.0), ("b", 9.0, 99.0)), duration=10.0)
    assert [(w.word, w.start, w.end) for w in out] == [("a", 0.0, 1.0), ("b", 9.0, 10.0)]


def test_clean_is_stable_for_equal_starts_and_capped():
    many = [CaptionWord(word=f"w{i}", start=float(i), end=float(i) + 0.5) for i in range(2005)]
    out = clean_words(many, duration=5000.0)
    assert len(out) <= 2000
    equal = clean_words(words(("b", 1.0, 2.0), ("a", 1.0, 3.0)), duration=10.0)
    # original order kept for identical starts (stable)
    assert [w.word for w in equal] == ["b", "a"]


# -- segmentation -------------------------------------------------------------------

def test_segment_chunks_by_words_per_line():
    ws = [CaptionWord(word=f"w{i}", start=10.0 + i, end=10.4 + i) for i in range(7)]
    lines = segment_lines(ws, CaptionStyle(words_per_line=3))
    assert [len(l.words) for l in lines] == [3, 3, 1]


def test_segment_breaks_on_silence_gap():
    ws = words(("a", 0.0, 1.0), ("b", 1.1, 2.0), ("c", 5.0, 6.0))
    lines = segment_lines(ws, STYLE)
    assert [len(l.words) for l in lines] == [2, 1]
    assert lines[1].start == 5.0


def test_segment_line_end_covers_k_windows_plus_tail():
    ws = words(("a", 0.0, 0.9), ("b", 0.9, 1.8), ("c", 1.8, 2.7))
    lines = segment_lines(ws, STYLE)
    assert len(lines) == 1
    line = lines[0]
    assert line.start == 0.0
    assert line.end >= 2.7  # tail after the last word
    assert sum(line.karak) == 270  # centiseconds
    assert all(k >= 1 for k in line.karak)


def test_segment_empty_is_empty():
    assert segment_lines([], STYLE) == []


# -- ASS serialization ---------------------------------------------------------------

def test_ass_color_converts_rgb_to_ass_bgr():
    assert ass_color("FFFFFF") == "&H00FFFFFF"
    assert ass_color("00D7FF") == "&H00FFD700"
    assert ass_color("aa11bb") == "&H00BB11AA"
    with pytest.raises(CaptionsError):
        ass_color("GGGGGG")
    with pytest.raises(CaptionsError):
        ass_color("FFF")


def test_format_ass_time():
    assert format_ass_time(0) == "0:00:00.00"
    assert format_ass_time(3661.23) == "1:01:01.23"
    assert format_ass_time(-5) == "0:00:00.00"
    assert format_ass_time(59.996) == "0:00:59.99" or format_ass_time(59.996) == "0:01:00.00"


def test_ass_escape_markup_chars():
    assert ass_escape("a{b}c\\d") == "a\\{b\\}c\\\\d"
    assert ass_escape("plain") == "plain"


def test_filter_escape_path_specials():
    # C:\dir, x'\f  ->  every \ : , ; [ ] = ' prefixed with a backslash
    assert filter_escape("C:\\dir, x'\\f") == "C\\:\\\\dir\\, x\\'\\\\f"
    assert filter_escape("/tmp/job/captions.ass") == "/tmp/job/captions.ass"


def test_build_ass_header_and_dialogues():
    ws = words(("one", 0.0, 1.0), ("two", 1.0, 2.0), ("three", 2.0, 3.0))
    lines = segment_lines(clean_words(ws, 10.0), STYLE)
    ass = build_ass(lines, STYLE, 1080, 1920)
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    assert "ScriptType: v4.00+" in ass
    assert "DejaVu Sans,88" in ass
    assert "&H00FFFFFF" in ass  # base color (Primary)
    assert "&H00FFD700" in ass  # highlight 00D7FF (Secondary)
    assert "MarginV" in ass
    dialogues = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogues) == len(lines)
    # every word carries a \k window
    text = dialogues[0]
    assert "{\\k100}ONE {\\k100}TWO {\\k100}THREE" in text  # uppercase latin
    # alignment 2 (bottom-center) is in the style line
    assert ",2,40,40,520,1" in ass


def test_build_ass_is_deterministic_bytes():
    ws = words(("a", 0.0, 1.0), ("b", 1.0, 2.0))
    lines = segment_lines(clean_words(ws, 10.0), STYLE)
    assert build_ass(lines, STYLE, 1080, 1920) == build_ass(lines, STYLE, 1080, 1920)


def test_build_ass_no_lines_no_dialogues():
    ass = build_ass([], STYLE, 1080, 1920)
    assert "Dialogue:" not in ass
    assert "[Events]" in ass


def test_build_ass_arabic_not_uppercased_and_logical_order_kept():
    ws = words(("مرحبا", 0.0, 1.0), ("العالم", 1.0, 2.0))
    lines = segment_lines(clean_words(ws, 10.0), STYLE)
    ass = build_ass(lines, STYLE, 1080, 1920)
    assert "مرحبا" in ass and "العالم" in ass
    # logical order preserved in the file (libass/fribidi do the visual bidi)
    assert ass.index("مرحبا") < ass.index("العالم")


def test_build_captions_ass_returns_none_without_valid_words():
    assert build_captions_ass([], STYLE, 10.0) is None
    assert build_captions_ass(words(("  ", 1.0, 2.0), ("x", 3.0, 3.0)), STYLE, 10.0) is None
    assert build_captions_ass(words(("a", -5.0, -1.0)), STYLE, 10.0) is None
    # default style when None
    out = build_captions_ass(words(("a", 1.0, 2.0)), None, 10.0)
    assert out is not None and "Dialogue:" in out


# -- persisted payload parser -----------------------------------------------------------

def test_parse_payload_valid_dict_clip_relative():
    raw = {"words": [{"w": "hallo", "s": 60.5, "e": 61.2}, {"w": "welt", "s": 62.0, "e": 62.8}]}
    out = parse_caption_payload(raw, clip_start=60.0, duration=10.0)
    assert [(w.word, w.start, w.end) for w in out] == [
        ("hallo", 0.5, 1.2), ("welt", 2.0, 2.8),
    ]


def test_parse_payload_accepts_caption_word_objects():
    raw = {"words": [CaptionWord(word="a", start=1.0, end=2.0)]}
    out = parse_caption_payload(raw, 0.0, 10.0)
    assert [(w.word, w.start, w.end) for w in out] == [("a", 1.0, 2.0)]


@pytest.mark.parametrize("raw", [None, "junk", 42, ["a"], {"no_words": 1}, {"words": "x"}])
def test_parse_payload_malformed_shape_is_empty(raw):
    assert parse_caption_payload(raw, 0.0, 10.0) == []


def test_parse_payload_drops_bad_entries_keeps_good():
    raw = {"words": [
        {"w": "ok", "s": 1.0, "e": 2.0},
        {"s": 3.0, "e": 4.0},            # no word
        {"w": 5, "s": 5.0, "e": 6.0},    # non-string word
        {"w": "bad", "s": "x", "e": 6.0},  # non-numeric
        {"w": "bad2", "s": 7.0},          # missing end
        "not-a-dict",
        {"w": "out", "s": 99.0, "e": 100.0},  # outside the 10s window
    ]}
    out = parse_caption_payload(raw, clip_start=0.0, duration=10.0)
    assert [(w.word, w.start, w.end) for w in out] == [("ok", 1.0, 2.0)]
