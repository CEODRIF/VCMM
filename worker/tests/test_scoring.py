"""Explainable scoring tests: value bounds, reasons, ordering, determinism."""

from __future__ import annotations

from app.ai.clip_detection import Sentence
from app.ai.scoring import (
    CRITERIA,
    CriterionScore,
    score_candidate,
    score_coherence,
    score_duration_fit,
    score_hook,
    scoring_payload,
    weighted_total,
)
from app.transcription.base import TranscriptionWord


def test_candidate_scores_all_five_criteria_with_reasons():
    sentences = [Sentence("What if you could double your output?", 0.0, 3.0),
                 Sentence("Here is the first step you should take today.", 3.0, 8.0)]
    words = tuple(TranscriptionWord(w, i * 0.4, i * 0.4 + 0.35) for i, w in enumerate(("a " * 40).split()))
    criterions = score_candidate(0.0, 8.0, sentences, list(words), target_duration=8.0)
    assert [c.name for c in criterions] == [c[0] for c in CRITERIA]
    assert sum(c.weight for c in criterions) == 1.0
    for c in criterions:
        assert 0.0 <= c.value <= 1.0
        assert c.reason.strip()
        assert isinstance(c, CriterionScore)


def test_question_hook_beats_flat_hook():
    question = [Sentence("What if 5 minutes a day could change your career?", 0.0, 4.0)]
    flat = [Sentence("The building is on the other side of the street and it is tall.", 0.0, 4.0)]
    assert score_hook(question).value > score_hook(flat).value
    assert "question" in score_hook(question).reason.lower()


def test_duration_fit_exact_target_is_perfect():
    assert score_duration_fit(90.0, 90.0).value == 1.0
    assert score_duration_fit(92.0, 90.0).value == 1.0  # within +5%
    assert score_duration_fit(112.5, 90.0).value == 0.0  # at -25% decay floor
    mid = score_duration_fit(100.0, 90.0)
    assert 0.0 < mid.value < 1.0
    assert "longer" in mid.reason


def test_coherence_rewards_continuous_speech():
    continuous = tuple(
        TranscriptionWord(f"w{i}", i * 0.5, i * 0.5 + 0.45) for i in range(40)
    )
    # Insert a 12s dead-air hole in the middle (second half shifted +12s),
    # same total window for a fair comparison.
    gappy = list(continuous[:20]) + [
        TranscriptionWord(f"t{i}", 22.0 + (i - 20) * 0.5, 22.0 + (i - 20) * 0.5 + 0.45)
        for i in range(20, 40)
    ]
    end = 32.0  # covers both halves (tail ends at 31.95)
    assert score_coherence(list(continuous), 0.0, end).value > score_coherence(gappy, 0.0, end).value
    assert "pause" in score_coherence(gappy, 0.0, end).reason.lower()


def test_weighted_total_scales_to_100():
    perfect = [CriterionScore(name, label, weight, 1.0, "ok") for name, label, weight in CRITERIA]
    zero = [CriterionScore(name, label, weight, 0.0, "ok") for name, label, weight in CRITERIA]
    assert weighted_total(perfect) == 100.0
    assert weighted_total(zero) == 0.0
    half = [CriterionScore(name, label, weight, 0.5, "ok") for name, label, weight in CRITERIA]
    assert weighted_total(half) == 50.0


def test_scoring_payload_shape():
    criterions = score_candidate(0.0, 90.0,
                                  [Sentence("One.", 0.0, 1.0), Sentence("Two.", 1.0, 2.0)],
                                  [], target_duration=90.0)
    payload = scoring_payload(criterions)
    assert payload["scale"] == "0-100"
    assert 0 <= payload["total"] <= 100
    assert len(payload["criteria"]) == 5
    for item in payload["criteria"]:
        assert set(item) == {"name", "label", "weight", "value", "reason"}
        assert item["reason"].strip()


def test_scoring_is_deterministic():
    sentences = [Sentence("Here is step one and it is important.", 0.0, 5.0),
                 Sentence("Why does this matter to you today?", 5.0, 9.0)]
    words = tuple(TranscriptionWord(w, i * 0.3, i * 0.3 + 0.28) for i, w in enumerate(("x " * 30).split()))
    a = score_candidate(0.0, 9.0, sentences, list(words), 90.0)
    b = score_candidate(0.0, 9.0, sentences, list(words), 90.0)
    assert [c.value for c in a] == [c.value for c in b]
    assert [c.reason for c in a] == [c.reason for c in b]


def test_empty_clip_scores_low_but_explained():
    criterions = score_candidate(0.0, 1.0, [], [], target_duration=90.0)
    assert all(0.0 <= c.value <= 1.0 for c in criterions)
    assert all(c.reason.strip() for c in criterions)
