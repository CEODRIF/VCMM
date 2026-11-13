"""Explainable clip scoring (Phase 4).

Every candidate is scored on five weighted criteria, each producing a
0-1 value AND a human-readable reason. The weighted total is scaled to
0-100 and persisted in ``clips.score`` / ``clips.scoring``.

The scoring is purely textual/timing-based — it consumes the real
transcription only (no LLM, no fabricated signal) and is deterministic:
the same transcript always yields the same scores.

Criteria (weights sum to 1.0):

- ``hook``         (0.30) — strength of the opening line.
- ``completeness`` (0.22) — the clip starts and ends at clean sentence
  boundaries and does not cut a word mid-way.
- ``coherence``    (0.18) — speech continuity: little dead air inside
  the clip (the story stays connected).
- ``engagement``   (0.20) — questions, direct address, list structures,
  concrete numbers: patterns that hold short-form attention.
- ``duration_fit`` (0.10) — closeness of the clip length to the duration
  the user requested.
"""

from __future__ import annotations

from dataclasses import dataclass

#: (name, label, weight) — weights sum to 1.0.
CRITERIA: tuple[tuple[str, str, float], ...] = (
    ("hook", "Opening hook strength", 0.30),
    ("completeness", "Semantic completeness", 0.22),
    ("coherence", "Context & coherence", 0.18),
    ("engagement", "Audience engagement potential", 0.20),
    ("duration_fit", "Duration suitability", 0.10),
)

_HOOK_QUESTION_OPENERS = (
    "what", "how", "why", "when", "where", "who", "can", "could", "should",
    "do", "does", "did", "is", "are", "was", "were",
    "que", "qu'est-ce", "quel", "quelle", "comment", "pourquoi", "est-ce",
)
_TENSION_PHRASES = (
    "secret", "mistake", "truth", "never", "always", "surprise", "actually",
    "le fait", "erreur", "jamais", "toujours",
)
_ENGAGEMENT_MARKERS = (
    "imagine", "picture this", "here's", "here is", "the truth", "fact",
    "number one", "first", "second", "third", "step", "secret", "mistake",
    "why", "because", "actually", "attention", "voici", "le fait",
    "numéro un", "premièrement", "deuxièmement", "surprend", "réponse",
)
_DIRECT_ADDRESS = ("you", "your", "tu", "ton", "ta", "vos", "vous")


@dataclass(frozen=True)
class CriterionScore:
    """One scored criterion: a 0-1 value plus the reason behind it."""

    name: str
    label: str
    weight: float
    value: float  # 0.0 .. 1.0
    reason: str  # human-readable, shown to the user

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "weight": self.weight,
            "value": round(self.value, 4),
            "reason": self.reason,
        }


def _criterion(index: int, value: float, *parts: str) -> CriterionScore:
    name, label, weight = CRITERIA[index]
    return CriterionScore(name=name, label=label, weight=weight,
                          value=round(_clamp(value), 4), reason=_reason(*parts))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _reason(*parts: str) -> str:
    """Joins reason fragments into one tidy, capitalized sentence."""
    cleaned = [p.strip() for p in parts if p and p.strip()]
    if not cleaned:
        return "No spoken text to evaluate."
    joined = "; ".join(cleaned).rstrip(".")
    return joined[0].upper() + joined[1:] + "."


def score_hook(sentences: list) -> CriterionScore:
    """Opening hook strength from the FIRST sentence of the clip.

    Signals: interrogative/curiosity openers, a question mark, direct
    address, concrete numbers, tension phrases — plus brevity (a short
    first line hits harder).
    """
    if not sentences:
        return _criterion(0, 0.0, "the clip has no spoken opening line")
    first = sentences[0].text.strip()
    lowered = first.lower()
    words = first.split()
    score = 0.25  # any spoken opening line is a baseline
    notes: list[str] = []

    if words and words[0].lower() in _HOOK_QUESTION_OPENERS:
        score += 0.30
        notes.append("opens with a question or curiosity trigger")
    if "?" in first:
        score += 0.15
        notes.append("asks a question the viewer wants answered")
    if any(w.lower() in _DIRECT_ADDRESS for w in words[:6]):
        score += 0.15
        notes.append("speaks to the viewer directly")
    if any(ch.isdigit() for ch in first[:24]):
        score += 0.10
        notes.append("uses a concrete number up front")
    if any(p in lowered for p in _TENSION_PHRASES):
        score += 0.10
        notes.append("contains a tension or pattern-break phrase")

    if len(words) <= 8:
        score += 0.15
        notes.append("short, punchy opening line")
    elif len(words) > 18:
        score -= 0.10
        notes.append("opening line is long before it lands")

    if not notes:
        notes.append("plain declarative opening")
    return _criterion(0, score, *notes[:3])


def score_completeness(sentences: list) -> CriterionScore:
    """Does the clip read as a complete thought (clean entry + exit)?"""
    if not sentences:
        return _criterion(1, 0.0, "the clip contains no complete sentence")
    score = 0.40
    notes = ["starts on a sentence boundary"]

    last = sentences[-1].text.strip()
    if last.endswith((".", "!", "?")):
        score += 0.35
        notes.append("ends on a full stop — the idea is finished")
    elif last and last[0].isupper():
        score += 0.25
        notes.append("ends on a natural clause break")
    else:
        score += 0.10
        notes.append("ends mid-sentence")

    total_words = sum(len(s.text.split()) for s in sentences)
    if total_words >= 30:
        score += 0.15
        notes.append("enough material to stand alone")
    else:
        score -= 0.15
        notes.append("very little spoken material")
    return _criterion(1, score, *notes[:4])


def score_coherence(words: list, start: float, end: float) -> CriterionScore:
    """Speech continuity: how little dead air inside the clip.

    Uses the REAL word timestamps: gaps between consecutive words that
    far exceed the typical word spacing indicate a context break.
    """
    inside = [w for w in words if start <= w.start < end]
    span = max(0.001, end - start)
    if len(inside) < 4:
        return _criterion(2, 0.2, "too little continuous speech to sustain context")
    gaps = [inside[i + 1].start - inside[i].end for i in range(len(inside) - 1)
            if inside[i + 1].start >= inside[i].end]
    spoken = sum(max(0.0, w.end - w.start) for w in inside)
    talk_ratio = _clamp(spoken / span)
    if not gaps:
        return _criterion(2, 0.8, "words run continuously with no context breaks")
    gaps.sort()
    typical = gaps[len(gaps) // 2]  # median gap
    breaks = sum(1 for g in gaps if g > max(2.5, 6.0 * typical))
    if breaks == 0:
        return _criterion(2, 0.85 * (0.5 + 0.5 * talk_ratio),
                          f"continuous speech ({round(talk_ratio * 100)}% of the clip is spoken)")
    score = (0.85 - 0.30 * breaks) * (0.5 + 0.5 * talk_ratio)
    return _criterion(2, score,
                      f"{breaks} long pause{'s' if breaks > 1 else ''} reduce context continuity")


def score_engagement(sentences: list) -> CriterionScore:
    """Short-form attention signals across the whole clip text."""
    if not sentences:
        return _criterion(3, 0.0, "no spoken text to evaluate")
    text = " ".join(s.text for s in sentences).lower()
    all_words = [w for s in sentences for w in s.text.split()]
    score = 0.30
    notes: list[str] = []
    questions = sum(s.text.count("?") for s in sentences)
    if questions:
        score += min(0.30, 0.12 * questions)
        notes.append(f"asks {questions} question{'s' if questions > 1 else ''}")
    hits = sum(1 for marker in _ENGAGEMENT_MARKERS if marker in text)
    if hits:
        score += min(0.30, 0.10 * hits)
        notes.append("uses engagement patterns (lists, facts, direct speech)")
    numbers = sum(1 for w in all_words if any(ch.isdigit() for ch in w))
    if numbers:
        score += min(0.10, 0.02 * numbers)
        notes.append("references concrete details")
    if not notes:
        notes.append("steady narrative without strong hooks")
    return _criterion(3, score, *notes[:3])


def score_duration_fit(duration: float, target: float) -> CriterionScore:
    """Closeness of the clip length to the requested duration.

    1.0 inside ±5% of the target, decaying linearly to 0 at ±25%.
    """
    if target <= 0:
        return _criterion(4, 0.5, "no target duration to compare against")
    ratio = duration / target
    if 0.95 <= ratio <= 1.05:
        return _criterion(4, 1.0, f"{duration:.0f}s is right on the requested {target:.0f}s")
    value = _clamp(1.0 - abs(ratio - 1.0) / 0.25, 0.0, 0.95)
    direction = "longer" if ratio > 1 else "shorter"
    return _criterion(4, value, f"{duration:.0f}s is {direction} than the requested {target:.0f}s")


def score_candidate(
    start: float,
    end: float,
    sentences: list,
    words: list,
    target_duration: float,
) -> list[CriterionScore]:
    """Scores one candidate on all five criteria (deterministic)."""
    return [
        score_hook(sentences),
        score_completeness(sentences),
        score_coherence(words, start, end),
        score_engagement(sentences),
        score_duration_fit(max(0.0, end - start), target_duration),
    ]


def weighted_total(criterions: list[CriterionScore]) -> float:
    """0-100 ranking score from the per-criterion 0-1 values."""
    total = sum(c.weight * c.value for c in criterions)
    return round(_clamp(total) * 100.0, 2)


def scoring_payload(criterions: list[CriterionScore]) -> dict:
    """The JSON persisted in ``clips.scoring`` (explainability for the user)."""
    return {
        "total": weighted_total(criterions),
        "scale": "0-100",
        "criteria": [c.as_dict() for c in criterions],
    }
