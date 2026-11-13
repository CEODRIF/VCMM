"""Bounded event graph + sequence extraction (TASK 010, layer 3a).

A deterministic, bounded representation — NOT a general-purpose graph
database:

- **nodes** = the cluster's kept observations (capped at
  ``max_signals_per_cluster`` upstream);
- **edges** = temporal relations between pairs of nodes whose STARTS are
  within ``correlation_window_seconds`` (overlap / precede / followed-by),
  capped at ``max_edges_per_node`` (deterministic: earliest neighbors
  first);
- **sequences** = ordered distinct-kind chains (length 2..
  ``max_sequence_length``), extracted greedily in time order, capped at
  ``max_sequences_per_cluster`` per cluster.

Order tolerance: a sequence step must START within
``step_window_seconds`` of the previous step's END (never re-require
exact detector order — timing uncertainty is respected), which is what
lets e.g. ``gameplay_change → kill_feed_entry → audio_reaction`` match
even when two stages start within a frame of each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import MultimodalConfig
from .correlation import TemporalCluster
from .timeline import NormalSignal

REL_OVERLAP = "overlap"          # intervals intersect
REL_PRECEDES = "precedes"        # a ends before b starts (within window)
REL_FOLLOWS = "follows"          # a starts after b (within window)


@dataclass
class Edge:
    a: str            # node key (event_key)
    b: str
    relation: str     # REL_*
    gap: float        # start-gap seconds (0 when overlapping)


@dataclass
class EventGraph:
    """Bounded nodes/edges for one cluster (deterministic ordering)."""

    nodes: list[str] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def explanation(self) -> str:
        """A short, human-readable temporal explanation (bounded)."""
        if not self.nodes:
            return "empty"
        kinds_by_key = self._kinds
        chain = " → ".join(kinds_by_key.get(k, "?") for k in self.nodes[:8])
        if len(self.nodes) > 8:
            chain += f" … (+{len(self.nodes) - 8} more)"
        return chain

    _kinds: dict[str, str] = field(default_factory=dict, repr=False)


def build_graph(cluster: TemporalCluster, cfg: MultimodalConfig) -> EventGraph:
    events = cluster.events
    graph = EventGraph()
    by_key: dict[str, NormalSignal] = {}
    for s in events:
        if s.key in by_key:
            continue
        by_key[s.key] = s
        if len(graph.nodes) < cfg.max_signals_per_cluster:
            graph.nodes.append(s.key)
    graph._kinds = {k: s.kind for k, s in by_key.items()}

    node_set = set(graph.nodes)
    for i, key_a in enumerate(graph.nodes):
        a = by_key[key_a]
        neighbors = 0
        for key_b in graph.nodes[i + 1:]:
            b = by_key[key_b]
            gap = b.t - a.t  # time-ordered → non-negative
            if gap > cfg.correlation_window_seconds:
                break  # sweep: later nodes are even farther
            if a.t_end > b.t:
                relation = REL_OVERLAP
                gap = 0.0
            elif a.t_end <= b.t:
                relation = REL_PRECEDES
            else:
                relation = REL_FOLLOWS
            graph.edges.append(Edge(a=key_a, b=key_b, relation=relation,
                                    gap=round(min(gap, cfg.correlation_window_seconds), 3)))
            neighbors += 1
            if neighbors >= cfg.max_edges_per_node:
                break
    return graph


def extract_sequences(
    cluster: TemporalCluster,
    graph: EventGraph,
    cfg: MultimodalConfig,
    step_window_seconds: float,
) -> list[list[str]]:
    """Ordered distinct-KIND chains (length >= 2) with per-step tolerance.

    Deterministic greedy walk over time-ordered kept signals:
    - consecutive identical kinds are skipped (a persistent kill-feed
      line observed over several samples is ONE stage, not many);
    - a step may start up to ``step_window_seconds`` after the previous
      step ended (timing uncertainty — exact order not re-required);
    - the current chain is emitted when broken; capped at
      ``max_sequences_per_cluster`` and length ``max_sequence_length``.
    """
    events = cluster.events
    sequences: list[list[str]] = []
    chain: list[tuple[str, float, float]] = []  # (kind, t, t_end)

    def _flush() -> None:
        if len(chain) >= 2:
            seq = [k for k, _t, _te in chain][: cfg.max_sequence_length]
            if seq not in sequences:
                sequences.append(seq)
        chain.clear()

    for s in events:
        if not chain:
            chain.append((s.kind, s.t, s.t_end))
            continue
        last_kind, last_t, last_end = chain[-1]
        if s.kind == last_kind:
            # same stage continuing: extend its end, do not repeat
            chain[-1] = (last_kind, last_t, max(last_end, s.t_end))
            continue
        if s.t - last_end <= step_window_seconds:
            if len(chain) >= cfg.max_sequence_length:
                _flush()
            chain.append((s.kind, s.t, s.t_end))
        else:
            _flush()
            chain.append((s.kind, s.t, s.t_end))
        if len(sequences) >= cfg.max_sequences_per_cluster:
            return sequences[: cfg.max_sequences_per_cluster]  # cap: stop early
    _flush()
    return sequences[: cfg.max_sequences_per_cluster]
