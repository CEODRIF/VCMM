"""Operational observability (TASK 012, spec section 6).

- ``metrics`` — lightweight in-process counters/gauges with BOUNDED
  cardinality, exposed as Prometheus text format (zero dependencies, no
  paid services);
- ``context`` — per-job structured log context (job_id, worker_id,
  correlation_id, stage, attempt, error_code) via contextvars.

Secrets are never logged: the JSON formatter applies the provider-layer
redaction to every message and exception it emits.
"""

from .context import job_context  # noqa: F401
from . import metrics  # noqa: F401
