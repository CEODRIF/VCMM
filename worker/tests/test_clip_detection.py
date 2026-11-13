"""Clip detection tests: duration band, sentence boundaries, overlap control,
determinism, honest failures — all on synthetic timed transcripts."""

from __future__ import annotations

import pytest

from app.ai.clip_detection import (
    ClipCandidate,
    NoClipsDetectedError,
    TranscriptClipDetector,
    extract_sentences,
)
from app.ai.scoring import CRITERIA
from app.transcription.base import TranscriptionSegment, TranscriptionWord

TARGET = 90
BAND = (76.5, 112.5)  # max(60, 0.85*90) .. min(180, 1.25*90)


def _talk(total_seconds: int = 300, sentence_seconds: int = 5, spoken: float = 4.0,
          with_words: bool = True, first_offset: float = 0.0) -> list[TranscriptionSegment]:
    """Deterministic talk: one 4s sentence every 5s (1s pause)."""
    patterns = [
        "Point number {k} matters because it explains step {k} in detail.",
        "What if you could learn secret {k} in one go?",
        "Here is fact number {k} that most people miss.",
        "First, you should notice signal {k} clearly.",
        "The truth about habit {k} is simpler than you think.",
    ]
    segments: list[TranscriptionSegment] = []
    k = 0
    t = first_offset
    while t + spoken <= total_seconds:
        text = patterns[k % len(patterns)].format(k=k + 1)
        tokens = text.split()
        share = spoken / len(tokens)
        words = tuple(
            TranscriptionWord(word=w, start=t + i * share, end=t + (i + 1) * share)
            for i, w in enumerate(tokens)
        ) if with_words else ()
        segments.append(TranscriptionSegment(text=text, start=t, end=t + spoken, words=words))
        k += 1
        t += sentence_seconds
    return segments


def test_detects_requested_number_within_duration_band():
    detector = TranscriptClipDetector()
    clips = detector.detect_scored(_talk(), 3, TARGET)
    assert len(clips) == 3
    for clip in clips:
        length = clip.end - clip.start
        assert BAND[0] <= length <= BAND[1]
        assert 60 <= length <= 180  # the DB bound, always
    # Sorted by start, unique.
    starts = [c.start for c in clips]
    assert starts == sorted(starts)
    assert len(set(starts)) == 3


def test_clips_start_and_end_on_sentence_boundaries():
    segments = _talk()
    detector = TranscriptClipDetector()
    clips = detector.detect_scored(segments, 3, TARGET)
    sentences = extract_sentences(segments)
    starts = {s.start for s in sentences}
    ends = {s.end for s in sentences}
    for clip in clips:
        assert clip.start in starts, f"clip start {clip.start} is not a sentence start"
        assert clip.end in ends, f"clip end {clip.end} is not a sentence end"


def test_no_pair_of_clips_overlaps_by_half_or_more():
    detector = TranscriptClipDetector()
    clips = detector.detect_scored(_talk(), 10, TARGET)
    for i, a in enumerate(clips):
        for b in clips[i + 1:]:
            overlap = min(a.end, b.end) - max(a.start, b.start)
            shorter = min(a.end - a.start, b.end - b.start)
            assert overlap / shorter < TranscriptClipDetector.MAX_OVERLAP_RATIO


def test_detection_is_deterministic():
    detector = TranscriptClipDetector()
    first = detector.detect_scored(_talk(), 3, TARGET)
    second = detector.detect_scored(_talk(), 3, TARGET)
    assert [(c.start, c.end, c.score, c.title) for c in first] == \
           [(c.start, c.end, c.score, c.title) for c in second]


def test_empty_transcript_raises():
    detector = TranscriptClipDetector()
    with pytest.raises(NoClipsDetectedError, match="(?i)no detectable speech"):
        detector.detect_scored([], 3, TARGET)


def test_short_talk_raises_honestly():
    # 30s of speech can never fill a 60-180s clip window.
    detector = TranscriptClipDetector()
    with pytest.raises(NoClipsDetectedError, match="(?i)no complete idea fits"):
        detector.detect_scored(_talk(total_seconds=30), 1, TARGET)


def test_requested_more_than_available_returns_honestly_fewer():
    detector = TranscriptClipDetector()
    clips = detector.detect_scored(_talk(total_seconds=300), 10, TARGET)
    assert 1 <= len(clips) < 10
    for clip in clips:
        assert BAND[0] <= clip.end - clip.start <= BAND[1]


def test_title_comes_from_first_sentence_and_is_bounded():
    detector = TranscriptClipDetector()
    clips = detector.detect_scored(_talk(), 3, TARGET)
    for clip in clips:
        assert 0 < len(clip.title) <= 80
        assert "\n" not in clip.title


def test_scoring_payload_is_explainable_and_complete():
    detector = TranscriptClipDetector()
    clips = detector.detect_scored(_talk(), 3, TARGET)
    for clip in clips:
        assert 0 <= clip.score <= 100
        names = [c.name for c in clip.criterions]
        assert names == [c[0] for c in CRITERIA]
        assert sum(c.weight for c in clip.criterions) == 1.0
        for criterion in clip.criterions:
            assert 0.0 <= criterion.value <= 1.0
            assert criterion.reason.strip()
        # Payload persisted in clips.scoring.
        payload = clip.scoring
        assert payload["scale"] == "0-100"
        assert abs(payload["total"] - clip.score) < 0.01
        assert [c["name"] for c in payload["criteria"]] == names
        for item in payload["criteria"]:
            assert item["reason"].strip()


def test_word_timestamps_determine_exact_clip_edges():
    # Each segment starts 0.25s BEFORE its first word (leading silence): a
    # clip beginning at the first sentence must start at 0.25 (word timing),
    # never at 0.0 (segment/grid timing). The strong question hook makes the
    # first window deterministically the best candidate.
    hook = "What if 5 minutes a day could change your career?"
    segments: list[TranscriptionSegment] = []
    t = 0.0
    for k in range(20):
        text = hook if k == 0 else f"Statement number {k + 1} continues the story about habit {k + 1}."
        tokens = text.split()
        share = 4.0 / len(tokens)
        words = tuple(
            TranscriptionWord(word=w, start=t + 0.25 + i * share, end=t + 0.25 + (i + 1) * share)
            for i, w in enumerate(tokens)
        )
        segments.append(TranscriptionSegment(text=text, start=t, end=t + 4.0, words=words))
        t += 5.0
    detector = TranscriptClipDetector()
    clips = detector.detect_scored(segments, 1, TARGET)
    assert clips and clips[0].start == 0.25, \
        f"expected the clip to start on the first word at 0.25, got {[c.start for c in clips]}"


def test_proportional_fallback_without_word_timestamps():
    detector = TranscriptClipDetector()
    clips = detector.detect_scored(_talk(with_words=False), 2, TARGET)
    assert len(clips) == 2
    for clip in clips:
        assert BAND[0] <= clip.end - clip.start <= BAND[1]


def test_phase1_contract_still_works():
    detector = TranscriptClipDetector()
    candidates = detector.detect(_talk(), 2, TARGET)
    assert candidates and all(isinstance(c, ClipCandidate) for c in candidates)
    assert all(0 <= c.score <= 100 for c in candidates)


def test_target_60_and_180_stay_inside_db_bounds():
    detector = TranscriptClipDetector()
    for target in (60, 180):
        clips = detector.detect_scored(_talk(total_seconds=400), 2, target)
        for clip in clips:
            assert 60 <= clip.end - clip.start <= 180


def test_clips_carry_real_words_clipped_to_window():
    detector = TranscriptClipDetector()
    segments = _talk()
    clips = detector.detect_scored(segments, 2, TARGET)
    assert clips
    all_token_words = {t.word for seg in segments for t in seg.words}
    for c in clips:
        assert c.words, "a clip must carry its real words (Phase 5B captions)"
        for t in c.words:
            assert t.word in all_token_words  # never fabricated
            assert c.start <= t.start < t.end <= c.end
        for a, b in zip(c.words, c.words[1:]):
            assert a.end <= b.start  # non-overlapping, in order


def test_clips_without_word_timestamps_use_honest_proportional_tokens():
    detector = TranscriptClipDetector()
    clips = detector.detect_scored(_talk(with_words=False), 2, TARGET)
    for c in clips:
        assert c.words  # derived from the transcribed text, not invented
        for t in c.words:
            assert c.start <= t.start < t.end <= c.end
