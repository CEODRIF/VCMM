"""Temporal correlation + evidence independence (TASK 010, layer 2).

Deterministic, bounded, sweep-line (single pass over time-ordered
signals — NO unbounded all-pairs):

- **Clustering**: an event joins the open cluster when its START is
  within ``correlation_window_seconds`` of the cluster's last event END
  (same union-chain rule as Phase 7, so the temporal structure stays
  comparable). Events keep ABSOLUTE timestamps; chunk-boundary overlap
  is a non-issue here because the pipeline's midpoint ownership +
  exact event_key dedup already ran upstream (verified, not re-derived).
- **Duplicate suppression** (three tiers, all recorded in provenance):
  1. same stable event_key → exact duplicate (1 occurrence kept);
   2. same family + same kind within ``same_kind_merge_seconds`` of a
      kept observation → repeat of the SAME physical signal (e.g. two
      OCR samples of one persistent kill-feed line, repeated face
      landmarks of one continuous reaction) → corroboration only, max
      strength kept. For OCR observations that carry comparable
      observed content (text / score values), the content must AGREE —
      two different readings are never the same physical signal (they
      may even be contradictory);
  3. same family + different kind → RELATED observations: they still
     count as ONE independent modality (a loud burst and an
     audio-derived excitement event from the same detector are not two
     modalities).
- **Independence**: an INDEPENDENT observation is (a unique kept event)
  keyed by (family, kind) after tier-2 merging; INDEPENDENT MODALITY is
  a distinct family. Cross-family proximity is weighted by
  ``exp(-gap / decay_tau)`` (bounded 0..1, deterministic).

A cluster's event may span several frames/samples: tiers 2-3 make sure
that never inflates independence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import MultimodalConfig
from .timeline import NormalSignal


@dataclass
class Observation:
    """A kept (deduplicated) observation — possibly corroborated by
    repeats of the same physical signal."""

    signal: NormalSignal
    corroborations: list[NormalSignal] = field(default_factory=list)  # tier-2 repeats
    related: list[NormalSignal] = field(default_factory=list)         # tier-3 same-family


@dataclass
class TemporalCluster:
    """One correlated group of observations (bounded, time-ordered)."""

    observations: list[Observation] = field(default_factory=list)
    start: float = 0.0
    end: float = 0.0
    #: (family, kind) pairs kept as independent observations
    independent_observations: set[tuple[str, str]] = field(default_factory=set)
    #: distinct families represented (independent modalities)
    independent_modalities: set[str] = field(default_factory=set)
    #: cross-family proximity weights (deterministic, bounded 0..1)
    proximity_weights: list[float] = field(default_factory=list)
    duplicates_exact: int = 0
    duplicates_repeated: int = 0
    #: Signals that arrived after the cluster's node cap: kept in the
    #: scoring multiset (so scores stay identical to Phase 7) but excluded
    #: from independence, graph, sequences and patterns (bounded by design).
    overflow: list[NormalSignal] = field(default_factory=list)

    @property
    def events(self) -> list[NormalSignal]:
        """Every kept signal (primary + corroboration + related), EACH
        EXACTLY ONCE, time-ordered — the scoring multiset (plus overflow).
        Key-deduplicated because tier-3 ``related`` links point at sibling
        primaries, which must not be double-listed."""
        seen: set[str] = set()
        out: list[NormalSignal] = []
        for o in self.observations:
            for s in (o.signal, *o.corroborations, *o.related):
                if s.key not in seen:
                    seen.add(s.key)
                    out.append(s)
        out.sort(key=lambda s: (s.t, s.t_end, s.family, s.kind, s.key))
        return out


def _merge_gap(a: NormalSignal, b: NormalSignal, cfg: MultimodalConfig) -> bool:
    """Tier-2: same (family, kind) within the merge gap of either start."""
    return abs(b.t - a.t) <= cfg.same_kind_merge_seconds


def _observed_content(s: NormalSignal) -> tuple | None:
    """Stable identity of WHAT was observed (OCR text / score values).
    ``None`` = a state-like observation with no comparable content."""
    evd = s.original.evidence
    if "text" in evd:
        return ("text", str(evd["text"]).strip().casefold())
    if "before" in evd or "after" in evd:
        return ("score", str(evd.get("label")), str(evd.get("before")),
                str(evd.get("after")))
    return None


def _same_physical_signal(a: NormalSignal, b: NormalSignal) -> bool:
    """Tier-2 content rule: when both observations carry comparable
    observed content, it must agree; otherwise only the temporal rule
    applies."""
    ca, cb = _observed_content(a), _observed_content(b)
    if ca is not None and cb is not None:
        return ca == cb
    return True


def correlate(
    signals: list[NormalSignal], cfg: MultimodalConfig
) -> tuple[list[TemporalCluster], int]:
    """Sweep-line clustering + three-tier dedup.

    Returns ``(clusters, dropped)`` where ``dropped`` is the number of
    LATE clusters truncated by ``max_clusters`` (deterministic: the
    earliest ``max_clusters`` are kept). Bounded: a cluster's observations
    are capped at ``max_signals_per_cluster`` (earliest kept; later signals
    become related evidence of a same-family observation or ``overflow``).
    """
    clusters: list[TemporalCluster] = []
    open_cluster: TemporalCluster | None = None

    for s in signals:  # already time-ordered
        if open_cluster is None or s.t > open_cluster.end + cfg.correlation_window_seconds:
            open_cluster = TemporalCluster(start=s.t, end=s.t_end)
            clusters.append(open_cluster)
        c = open_cluster
        c.end = max(c.end, s.t_end)

        if len(c.observations) >= cfg.max_signals_per_cluster:
            # Bounded node cap: the signal is KEPT (scoring multiset) but
            # can never grow the graph or independence.
            same_family = next((o for o in c.observations
                                if o.signal.family == s.family), None)
            if same_family is not None:
                same_family.related.append(s)
            else:
                c.overflow.append(s)
            continue

        # 1) exact duplicate (stable key)
        same_key = next((o for o in c.observations if o.signal.key == s.key), None)
        if same_key is not None:
            c.duplicates_exact += 1
            same_key.corroborations.append(s)
            continue

        # 2) same family + same kind within merge gap (and, for OCR
        #    content, the SAME observed content) → repeat signal
        same_kind = next((o for o in c.observations
                          if o.signal.family == s.family and o.signal.kind == s.kind
                          and _merge_gap(o.signal, s, cfg)
                          and _same_physical_signal(o.signal, s)), None)
        if same_kind is not None:
            c.duplicates_repeated += 1
            same_kind.corroborations.append(s)
            continue

        # 3) new observation. Same family (different kind, or same kind
        #    beyond the merge gap) = RELATED: it still counts as ONE
        #    independent modality (the (family, kind) set and the family
        #    set never grow twice). Provenance links the sibling to the
        #    first same-family observation (one direction — the multiset
        #    is deduplicated by key in ``TemporalCluster.events``).
        same_family = next((o for o in c.observations
                            if o.signal.family == s.family), None)
        obs = Observation(signal=s)
        if same_family is not None:
            same_family.related.append(s)
        c.observations.append(obs)
        c.independent_observations.add((s.family, s.kind))
        c.independent_modalities.add(s.family)

    dropped = 0
    if len(clusters) > cfg.max_clusters:
        # Deterministic truncation: keep the EARLIEST max_clusters.
        dropped = len(clusters) - cfg.max_clusters
        del clusters[cfg.max_clusters:]
    _proximity_weights(clusters, cfg)
    return clusters, dropped


def _proximity_weights(clusters: list[TemporalCluster], cfg: MultimodalConfig) -> None:
    """Cross-family proximity per cluster: for each pair of INDEPENDENT
    observations of DIFFERENT families, weight = exp(-gap/tau) where gap
    is the start-gap (0 when overlapping). Bounded by construction; the
    list is capped so provenance stays small (deterministic: strongest
    weights first, then earliest)."""
    for c in clusters:
        weights: list[tuple[float, str, str]] = []
        obs = c.observations
        for i in range(len(obs)):
            for j in range(i + 1, len(obs)):
                a, b = obs[i].signal, obs[j].signal
                if a.family == b.family:
                    continue
                gap = max(0.0, max(a.t, b.t) - min(a.t_end, b.t_end))
                gap = min(gap, cfg.correlation_window_seconds * 2.0)  # bounded
                w = math.exp(-gap / cfg.decay_tau_seconds)
                weights.append((w, a.family, b.family))
        weights.sort(key=lambda x: (-x[0], x[1], x[2]))
        c.proximity_weights = [round(w, 4) for w, _f, _g in weights[:16]]
