"""AI clip detection on timed transcripts (Phase 4).

Splits the REAL transcription into sentence-level units (word timestamps,
with a proportional-timing fallback for providers that do not supply them),
then finds complete-idea windows inside the requested duration band:

- window length in ``[max(60, 0.85 * target), min(180, 1.25 * target)]``
  — always inside the product/DB bound of 60-180 seconds,
- windows start and end on SENTENCE boundaries (no cut mid-sentence),
- candidates are scored (see :mod:`app.ai.scoring`) and selected greedily
  so that no two kept clips overlap by 50% or more of the shorter one,
- the detector is deterministic and never invents content: if the speech
  is too short or too fragmented, it raises :class:`NoClipsDetectedError`
  and the job fails honestly instead of persisting fake clips.

Phase 1 compatibility: :class:`ClipCandidate` and :class:`BaseClipDetector`
keep their original shape; the Phase 4 implementation adds
:meth:`TranscriptClipDetector.detect_scored` which returns the per-criterion
explainability payload persisted in ``clips.scoring``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .scoring import CriterionScore, score_candidate, scoring_payload

if TYPE_CHECKING:
    from ..transcription.base import TranscriptionSegment


class NoClipsDetectedError(RuntimeError):
    """No suitable 60-180s complete-idea window exists in the transcript."""


# ---------------- Phase 1 contract (unchanged) -------------------------------

@dataclass(frozen=True)
class ClipCandidate:
    start: float  # seconds into the source video
    end: float  # seconds into the source video
    score: float
    title: str


class BaseClipDetector(ABC):
    """Interface for selecting the strongest short-form moments."""

    @abstractmethod
    def detect(
        self,
        segments: "list[TranscriptionSegment]",
        clip_count: int,
        duration_seconds: int,
    ) -> list[ClipCandidate]:
        """Select ``clip_count`` moments, each ~``duration_seconds`` long."""
        raise NotImplementedError


# ---------------- Phase 4 data model ------------------------------------------

@dataclass(frozen=True)
class Sentence:
    """One spoken sentence with absolute timing (seconds)."""

    text: str
    start: float
    end: float


@dataclass(frozen=True)
class TimedToken:
    """One word with absolute timing (real or proportionally derived)."""

    word: str
    start: float
    end: float


@dataclass(frozen=True)
class ScoredClip:
    """A detected clip plus its explainable scoring payload."""

    start: float
    end: float
    title: str
    score: float  # 0-100
    criterions: tuple[CriterionScore, ...] = field(default_factory=tuple)
    scoring: dict = field(default_factory=dict)
    #: Real transcribed words overlapping [start, end] (ABSOLUTE audio time),
    #: clipped to the window (Phase 5B captions). Never fabricated: only the
    #: transcribed tokens themselves (or their honest proportional
    #: interpolation when the provider gives no word timings).
    words: tuple[TimedToken, ...] = field(default_factory=tuple)

    def as_candidate(self) -> ClipCandidate:
        return ClipCandidate(start=self.start, end=self.end, score=self.score, title=self.title)


_SENTENCE_ENDS = (".", "!", "?", ":", ";", "。", "！")
_WS = re.compile(r"\s+")


def _sentence(text: str, start: float, end: float) -> Sentence | None:
    text = _WS.sub(" ", text).strip()
    if not text:
        return None
    return Sentence(text=text, start=round(start, 3), end=round(max(end, start), 3))


def _tokens_for_segment(segment: "TranscriptionSegment") -> list[TimedToken]:
    """Real word timings, or proportional timings derived from the text.

    The proportional fallback is an HONEST INTERPOLATION of where each word
    sits inside the timed segment — it never invents text: the words are the
    transcribed words themselves.
    """
    if segment.words:
        tokens: list[TimedToken] = []
        for w in segment.words:
            word = _WS.sub("", w.word)
            if not word:
                continue
            tokens.append(TimedToken(word=word, start=float(w.start), end=float(max(w.end, w.start))))
        return tokens
    words = _WS.split(segment.text.strip())
    if not words:
        return []
    span = max(0.001, segment.end - segment.start)
    share = span / len(words)
    return [
        TimedToken(word=w, start=segment.start + i * share, end=segment.start + (i + 1) * share)
        for i, w in enumerate(words)
    ]


def extract_sentences(segments: "list[TranscriptionSegment]") -> list[Sentence]:
    """Groups timed words into sentences.

    A sentence ends when a word carries sentence-final punctuation, or when
    the pause to the next word is far larger than typical word spacing
    (a breath or a topic change).
    """
    sentences: list[Sentence] = []
    for segment in segments:
        tokens = _tokens_for_segment(segment)
        current: list[TimedToken] = []
        for token in tokens:
            if current:
                prev = current[-1]
                gap = token.start - prev.end
                typical = max(0.05, prev.end - prev.start)
                ends_sentence = prev.word.endswith(_SENTENCE_ENDS)
                long_pause = gap > max(0.8, 4.0 * typical)
                if ends_sentence or long_pause:
                    built = _sentence(" ".join(t.word for t in current), current[0].start, prev.end)
                    if built:
                        sentences.append(built)
                    current = []
            current.append(token)
        if current:
            built = _sentence(" ".join(t.word for t in current), current[0].start, current[-1].end)
            if built:
                sentences.append(built)
    return sentences


def _window_bounds(target: float) -> tuple[float, float]:
    """(min_len, max_len) — always within the 60-180s product bound."""
    min_len = max(60.0, 0.85 * float(target))
    max_len = min(180.0, 1.25 * float(target))
    return min_len, max_len


def _candidate_windows(sentences: list[Sentence], min_len: float, max_len: float) -> list[tuple[int, int]]:
    """(start_idx, end_idx) sentence spans whose length falls in [min, max].

    For each start position the LONGEST valid span is kept (the most complete
    idea that still fits the band).
    """
    windows: list[tuple[int, int]] = []
    n = len(sentences)
    for i in range(n):
        start = sentences[i].start
        j = i
        # grow until the window is at least min_len
        while j + 1 < n and (sentences[j].end - start) < min_len:
            j += 1
        if (sentences[j].end - start) < min_len:
            continue  # not enough speech left from here on
        # grow as far as the band allows
        while j + 1 < n and (sentences[j + 1].end - start) <= max_len:
            j += 1
        if min_len <= sentences[j].end - start <= max_len:
            windows.append((i, j))
    return windows


def _overlap_ratio(a: ScoredClip, b: ScoredClip) -> float:
    overlap = min(a.end, b.end) - max(a.start, b.start)
    if overlap <= 0:
        return 0.0
    shorter = min(max(0.001, a.end - a.start), max(0.001, b.end - b.start))
    return overlap / shorter


def _title_for(sentences: list[Sentence]) -> str:
    """First sentence, collapsed to one line and truncated to 80 chars."""
    text = _WS.sub(" ", sentences[0].text).strip() if sentences else "Untitled clip"
    if len(text) <= 80:
        return text
    cut = text[:79]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() + "…"


class TranscriptClipDetector(BaseClipDetector):
    """Phase 4 detector: sentence windows + explainable scoring + overlap control."""

    #: Max allowed overlap between two kept clips (fraction of the shorter).
    MAX_OVERLAP_RATIO = 0.5

    def detect_scored(
        self,
        segments: "list[TranscriptionSegment]",
        clip_count: int,
        duration_seconds: int,
    ) -> list[ScoredClip]:
        """Detects, scores and de-duplicates the best ``clip_count`` windows.

        Raises :class:`NoClipsDetectedError` when the transcript contains no
        suitable window (honest failure — no fabricated clips).
        """
        if clip_count < 1:
            clip_count = 1
        sentences = extract_sentences(segments)
        if not sentences:
            raise NoClipsDetectedError("The recording contains no detectable speech.")
        min_len, max_len = _window_bounds(duration_seconds)
        windows = _candidate_windows(sentences, min_len, max_len)
        if not windows:
            total_speech = sentences[-1].end - sentences[0].start
            raise NoClipsDetectedError(
                f"Speech spans {total_speech:.0f}s of continuous content; "
                f"no complete idea fits the {min_len:.0f}-{max_len:.0f}s clip window."
            )

        all_words = [t for segment in segments for t in _tokens_for_segment(segment)]
        scored: list[ScoredClip] = []
        for i, j in windows:
            span_sentences = sentences[i : j + 1]
            start, end = span_sentences[0].start, span_sentences[-1].end
            criterions = score_candidate(start, end, span_sentences, all_words, float(duration_seconds))
            # Phase 5B: the clip's REAL words, clipped to the window (the
            # caption engine never invents text or timings).
            clip_words = tuple(
                TimedToken(
                    word=t.word,
                    start=round(max(t.start, start), 3),
                    end=round(min(t.end, end), 3),
                )
                for t in all_words
                if t.end > start and t.start < end and t.end > t.start
            )
            scored.append(
                ScoredClip(
                    start=round(start, 3),
                    end=round(end, 3),
                    title=_title_for(span_sentences),
                    score=round(sum(c.weight * c.value for c in criterions) * 100.0, 2),
                    criterions=tuple(criterions),
                    scoring=scoring_payload(criterions),
                    words=clip_words,
                )
            )

        scored.sort(key=lambda c: (-c.score, c.start))
        selected: list[ScoredClip] = []
        for candidate in scored:
            if all(_overlap_ratio(candidate, kept) < self.MAX_OVERLAP_RATIO for kept in selected):
                selected.append(candidate)
            if len(selected) >= clip_count:
                break
        selected.sort(key=lambda c: c.start)
        return selected

    def detect(
        self,
        segments: "list[TranscriptionSegment]",
        clip_count: int,
        duration_seconds: int,
    ) -> list[ClipCandidate]:
        """Phase 1 contract — plain candidates without the scoring payload."""
        return [clip.as_candidate() for clip in self.detect_scored(segments, clip_count, duration_seconds)]
