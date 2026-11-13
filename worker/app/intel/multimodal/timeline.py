"""Signal normalization layer (TASK 010, layer 1 of 5).

Wraps the EXISTING ``SignalEvent`` contract (no competing schema):

- every input keeps its stable ``event_key``, absolute timestamps,
  family/kind, original measured strength and original evidence payload
  (referenced, never mutated);
- values are validated again at the engine boundary (re-hydrated rows
  are untrusted data): malformed events are QUARANTINED — counted and
  recorded with a safe reason — never crash the job and never fabricate
  corrections;
- derived quantities (quality, detector identity) live only in the
  ``NormalSignal`` projection; the original evidence is preserved
  unchanged;
- deterministic ordering: (t, t_end, family, kind, key) — stable IDs.

Quality (0..1, DERIVED, never overrides the original strength):
- facecam: evidence carries measured values only → quality defaults to
  the strength (no separate detector confidence was measured);
- game vision: evidence carries OCR ``ocr_confidence`` (0..100) →
  quality = ocr_confidence / 100 (the detector's measured confidence);
- audio/visual: quality defaults to the strength.
``directly_observed`` (game vision) is preserved as a flag, never as a
quality boost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..signals import SIGNAL_FAMILIES, SignalEvent, event_key


@dataclass(frozen=True)
class NormalSignal:
    """A validated, normalized projection of one SignalEvent."""

    key: str                      # stable event_key (dedup identity)
    t: float                      # absolute source seconds (validated)
    t_end: float
    family: str
    kind: str
    strength: float               # ORIGINAL measured strength (0..1)
    detector: str | None          # engine/model identity from evidence
    quality: float                # derived 0..1 (see module docstring)
    directly_observed: bool | None  # as recorded by the detector (None = n/a)
    original: SignalEvent = field(repr=False, compare=False)  # reference, unchanged


@dataclass
class NormalizationResult:
    """Signals + an honest quarantine record (nothing silently dropped)."""

    signals: list[NormalSignal]
    quarantined: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False       # input exceeded max_events (time-ordered cut)

    @property
    def ok(self) -> bool:
        return not self.quarantined and not self.truncated


def _is_finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(float(v))


def _json_safe_number(v: Any) -> Any:
    """Finite numbers stay numbers; everything else becomes a short label
    (quarantine records are stored as jsonb and must always serialize)."""
    if _is_finite_number(v):
        return float(v)
    return "non-finite" if v is not None else None


def _quality_of(family: str, strength: float, evidence: dict[str, Any]) -> float:
    if family == "game_vision":
        conf = evidence.get("ocr_confidence")
        if _is_finite_number(conf):
            c = float(conf)
            if 0.0 <= c <= 100.0:  # tesseract scale
                return round(min(1.0, c / 100.0), 4)
            if 0.0 <= c <= 1.0:    # already normalized
                return round(c, 4)
    return round(min(1.0, max(0.0, strength)), 4)


def normalize_events(
    events: list[SignalEvent],
    job_id: str,
    max_events: int,
) -> NormalizationResult:
    """Validates + normalizes the persisted event list.

    - non-finite / out-of-range timestamps or strength → quarantined;
    - unknown family → quarantined (stored rows are untrusted data);
    - deterministic time-order truncation to ``max_events`` (recorded);
    - deterministic ordering of the survivors.
    """
    signals: list[NormalSignal] = []
    quarantined: list[dict[str, Any]] = []

    for i, e in enumerate(events):
        try:
            if not _is_finite_number(e.t) or not _is_finite_number(e.t_end):
                raise ValueError("non-finite timestamps")
            if not _is_finite_number(e.strength):
                raise ValueError("non-finite strength")
            if float(e.t) < 0.0 or float(e.t_end) < float(e.t):
                raise ValueError("timestamps violate 0 <= t <= t_end")
            if not 0.0 <= float(e.strength) <= 1.0:
                raise ValueError("strength outside [0, 1]")
            if e.family not in SIGNAL_FAMILIES:
                raise ValueError(f"unknown family {e.family!r}")
            if not isinstance(e.kind, str) or not e.kind:
                raise ValueError("missing kind")
        except (ValueError, TypeError, AttributeError) as exc:
            quarantined.append({
                "index": i,
                "reason": str(exc),
                "family": getattr(e, "family", None),
                "kind": getattr(e, "kind", None),
                # JSON-safe: a quarantined t may be non-finite (NaN/inf),
                # which is invalid jsonb — keep the record honest, not exact.
                "t": _json_safe_number(getattr(e, "t", None)),
            })
            continue

        evidence = e.evidence if isinstance(e.evidence, dict) else {}
        signals.append(NormalSignal(
            key=event_key(job_id, e),
            t=round(float(e.t), 3),
            t_end=round(float(e.t_end), 3),
            family=e.family,
            kind=e.kind,
            strength=round(float(e.strength), 4),
            detector=evidence.get("engine") or evidence.get("model"),
            quality=_quality_of(e.family, float(e.strength), evidence),
            directly_observed=(evidence.get("directly_observed")
                               if isinstance(evidence.get("directly_observed"), bool)
                               else None),
            original=e,
        ))

    truncated = False
    if len(signals) > max_events:
        # Deterministic: keep the EARLIEST max_events in time order.
        signals.sort(key=_order_key)
        signals = signals[:max_events]
        truncated = True
    else:
        signals.sort(key=_order_key)

    return NormalizationResult(signals=signals, quarantined=quarantined,
                               truncated=truncated)


def _order_key(s: NormalSignal) -> tuple[float, float, str, str, str]:
    return (s.t, s.t_end, s.family, s.kind, s.key)
