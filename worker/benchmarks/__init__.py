"""Deterministic benchmark framework for the stream-intelligence pipeline
(TASK 007).

- ``fixtures`` — a scripted, reproducible synthetic source with GROUND-TRUTH
  event timestamps (no network, no paid services, no committed binaries).
- ``run``      — the benchmark runner: interrupted run + resume, event
  precision/recall vs ground truth, duplicates, chunk-boundary correctness,
  throughput, peak memory, render validity. JSON report with MEASURED values
  clearly separated from labeled TARGETS.

Honesty rule: the fixture is SYNTHETIC. It measures pipeline correctness and
resource behavior. It does NOT establish real-world extraction accuracy —
that requires human-annotated real footage (documented as a gap).
"""
