"""Chunk planning tests (TASK 007) — pure math, deterministic, no I/O.

Covers: exact timestamps/boundaries, configured overlap, first/final chunks,
very long recordings (no product duration cap), invalid inputs, and the
midpoint ownership rule (every absolute instant has EXACTLY ONE owner —
the property that makes boundary events neither lost nor duplicated).
"""

from __future__ import annotations

import math

import pytest

from app.intel.chunk_plan import ChunkSpan, ownership_bounds, owning_span_index, plan_chunks


# ---- planning -----------------------------------------------------------------

def test_short_recording_is_a_single_span():
    spans = plan_chunks(120.0, 600, 30)
    assert len(spans) == 1
    assert spans[0].start == 0.0
    assert spans[0].end == 120.0
    assert spans[0].duration == 120.0


def test_exact_chunk_length_is_single_span():
    spans = plan_chunks(600.0, 600, 30)
    assert len(spans) == 1
    assert (spans[0].start, spans[0].end) == (0.0, 600.0)


def test_medium_recording_exact_timestamps():
    # 1200 s @ chunk 600 / overlap 30 → step 570: [0,600) [570,1170) [1140,1200)
    spans = plan_chunks(1200.0, 600, 30)
    assert [s.index for s in spans] == [0, 1, 2]
    assert [(s.start, s.end) for s in spans] == [(0.0, 600.0), (570.0, 1170.0), (1140.0, 1200.0)]


def test_overlap_is_exactly_configured():
    spans = plan_chunks(3000.0, 600, 45)
    for prev, nxt in zip(spans, spans[1:]):
        overlap = prev.end - nxt.start
        assert overlap == pytest.approx(45.0), f"expected 45s overlap, got {overlap}"


def test_first_and_final_chunks():
    total = 3600.0
    spans = plan_chunks(total, 600, 30)
    assert spans[0].start == 0.0
    assert spans[-1].end >= total - 0.5  # full coverage of the tail
    assert spans[-1].end <= total + 0.5  # ...without going past the end
    for s in spans:
        assert 0 < s.duration <= 600


def test_very_long_recording_has_no_duration_cap():
    # 100 hours must plan without any error and without any cap.
    total = 100 * 3600.0
    spans = plan_chunks(total, 600, 30)
    step = 570.0
    expected_count = math.ceil((total - 600) / step) + 1
    assert len(spans) == expected_count  # 632 spans
    assert [s.index for s in spans] == list(range(len(spans)))
    # Interior spans step exactly by (chunk - overlap); all are <= chunk long.
    for prev, nxt in zip(spans, spans[1:]):
        assert nxt.start - prev.start == pytest.approx(step)
        assert 0 < nxt.duration <= 600
    assert spans[0].start == 0.0
    assert spans[-1].end == pytest.approx(total, abs=0.5)


def test_zero_overlap_is_contiguous():
    spans = plan_chunks(1500.0, 600, 0)
    for prev, nxt in zip(spans, spans[1:]):
        assert nxt.start == pytest.approx(prev.end)


def test_invalid_inputs():
    with pytest.raises(ValueError):
        plan_chunks(0.0, 600, 30)
    with pytest.raises(ValueError):
        plan_chunks(-5.0, 600, 30)
    with pytest.raises(ValueError):
        plan_chunks(600.0, 0, 0)
    with pytest.raises(ValueError):
        plan_chunks(600.0, 600, 600)   # overlap == chunk
    with pytest.raises(ValueError):
        plan_chunks(600.0, 600, 900)   # overlap > chunk
    with pytest.raises(ValueError):
        plan_chunks(600.0, 600, -1)    # negative overlap


# ---- midpoint ownership ---------------------------------------------------------

def _plan():
    return plan_chunks(3000.0, 600, 30)


def test_ownership_bounds_are_contiguous_and_exhaustive():
    spans = _plan()
    bounds = ownership_bounds(spans, 30)
    assert len(bounds) == len(spans)
    # First owner starts at 0, last owner ends at +inf: the whole timeline is
    # partitioned — no instant is owned by zero or two chunks.
    assert bounds[0][0] == 0.0
    assert bounds[-1][1] == math.inf
    for (lo_a, hi_a), (lo_b, hi_b) in zip(bounds, bounds[1:]):
        assert hi_a == lo_b  # contiguous
    # Midpoint rule: each boundary is exactly half the overlap from the next start.
    for i, span in enumerate(spans):
        if i > 0:
            assert bounds[i][0] == pytest.approx(span.start + 15.0)


def test_ownership_at_chunk_boundaries_is_single():
    spans = _plan()
    bounds = ownership_bounds(spans, 30)
    # The straddle point t=585 (inside chunk 0 AND chunk 1) is owned by ONE span.
    t = spans[1].start + 15.0  # midpoint of the 0/1 overlap → boundary of ownership
    owners = [i for i, (lo, hi) in enumerate(bounds) if lo <= t < hi]
    assert owners == [1]
    # A hair before → chunk 0; a hair after → chunk 1.
    assert owning_span_index(t - 0.001, bounds) == 0
    assert owning_span_index(t + 0.001, bounds) == 1


def test_owning_span_index_out_of_range():
    spans = _plan()
    bounds = ownership_bounds(spans, 30)
    assert owning_span_index(-0.1, bounds) is None
    # The LAST owner runs to +inf by design (detectors only ever emit events
    # inside the recorded range, so an out-of-range probe past the end is
    # owned by the final span rather than "lost").
    assert owning_span_index(10 * 3600.0, bounds) == len(spans) - 1
    assert owning_span_index(1500.0, bounds) is not None


def test_every_instant_has_exactly_one_owner():
    # Sweep the whole timeline (incl. overlap regions): coverage of the
    # ownership partition is what guarantees boundary events are persisted
    # exactly once.
    spans = plan_chunks(9000.0, 600, 30)
    bounds = ownership_bounds(spans, 30)
    probed = [0.0, 14.999, 15.0, 15.001, 585.0, 599.999, 600.0, 8999.999]
    for t in probed:
        owners = [i for i, (lo, hi) in enumerate(bounds) if lo <= t < hi]
        assert len(owners) == 1, f"t={t} owned by {owners}"


def test_single_span_owns_whole_timeline():
    spans = plan_chunks(100.0, 600, 30)
    bounds = ownership_bounds(spans, 30)
    assert bounds == [(0.0, math.inf)]
    assert owning_span_index(99.9, bounds) == 0


def test_chunk_span_duration_property():
    s = ChunkSpan(0, 10.0, 610.0)
    assert s.duration == 600.0
