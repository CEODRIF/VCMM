"""In-process operational metrics (TASK 012, spec section 6).

Design constraints:

- ZERO dependencies (a stdlib-only registry; the endpoint renders the
  Prometheus text exposition format, which any scraper can parse).
- BOUNDED CARDINALITY, enforced twice: every family declares its label
  names and the code only ever passes values from a finite domain (job
  stages, error categories, provider names, outcomes); the registry
  additionally caps the number of label series per family and DROPS
  (and warns once) beyond the cap — a bug cannot create unbounded
  memory.
- Counters are monotonic per process; gauges reflect the current state.
  No histograms (mean = sum/count suffices at this scale).
- Thread/async safe via a single re-entrant lock; rendering is cheap
  (tens of series).
"""

from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict
from typing import Any

logger = logging.getLogger("clipforge.metrics")

#: Job stages (the only values ever used for ``stage`` labels).
STAGES = ("validating", "analysis", "rendering")

#: Map a jobs.status value to its coarse stage label (bounded domain).
_STATUS_TO_STAGE = {
    "validating": "validating",
    "transcribing": "analysis",
    "analyzing": "analysis",
    "rendering": "rendering",
}

#: Provider names for the validation-outcome metric (bounded domain).
PROVIDERS = ("youtube", "twitch", "upload")
PROVIDER_OUTCOMES = (
    "available", "restricted", "deleted", "rate_limited", "unavailable",
    "invalid_url", "unsupported", "error",
)

#: Storage operations instrumented (bounded domain).
STORAGE_OPERATIONS = ("download", "upload_clip", "upload_source")


def stage_for_status(status: str) -> str:
    """Bounded normalization: unknown statuses map to 'other' (never a
    free-form value — cardinality safety)."""
    return _STATUS_TO_STAGE.get(status, "other")


def _normalize(value: Any, allowed: tuple[str, ...], fallback: str = "other") -> str:
    value = str(value or "")
    return value if value in allowed else fallback


class _Family:
    """One metric family (a name + fixed label set + bounded series)."""

    __slots__ = ("name", "help", "type", "labels", "cap", "values", "overflow_logged")

    def __init__(self, name: str, help_text: str, type_: str,
                 labels: tuple[str, ...] = (), cap: int = 256) -> None:
        self.name = name
        self.help = help_text
        self.type = type_  # "counter" | "gauge"
        self.labels = labels
        self.cap = cap
        self.values: dict[tuple[str, ...], float] = defaultdict(float)
        self.overflow_logged = False

    def _key(self, labels: dict[str, Any] | None) -> tuple[str, ...] | None:
        labels = labels or {}
        for key in labels:
            if key not in self.labels:
                # Unknown label: refuse the update rather than grow series.
                return None
        key = tuple(str(labels.get(name, "")) for name in self.labels)
        return key

    def inc(self, labels: dict[str, Any] | None = None, amount: float = 1.0) -> None:
        if self.type != "counter":
            raise ValueError(f"{self.name} is not a counter")
        key = self._key(labels)
        if key is None:
            self._warn_overflow("unknown label name")
            return
        if key not in self.values and len(self.values) >= self.cap:
            self._warn_overflow(f"series cap ({self.cap}) reached")
            return
        self.values[key] += amount

    def set(self, value: float, labels: dict[str, Any] | None = None) -> None:
        if self.type != "gauge":
            raise ValueError(f"{self.name} is not a gauge")
        key = self._key(labels)
        if key is None:
            self._warn_overflow("unknown label name")
            return
        if key not in self.values and len(self.values) >= self.cap:
            self._warn_overflow(f"series cap ({self.cap}) reached")
            return
        self.values[key] = value

    def _warn_overflow(self, reason: str) -> None:
        if not self.overflow_logged:
            self.overflow_logged = True
            logger.warning("metric %s: dropping update (%s); cardinality stays bounded",
                           self.name, reason)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.type}"]
        if not self.values:
            # Emit one zero series so the metric is visible even before use.
            lines.append(f'{self.name}{self._format_labels({})} 0')
            return lines
        for key, value in sorted(self.values.items()):
            labels = self._format_labels(dict(zip(self.labels, key)))
            rendered = f"{value:.10g}"  # compact, parseable, no exponent surprises
            lines.append(f"{self.name}{labels} {rendered}")
        return lines

    def _format_labels(self, labels: dict[str, str]) -> str:
        if not self.labels:
            return ""
        inner = ",".join(
            f'{name}="{self._escape(str(labels.get(name, "")))}"' for name in self.labels
        )
        return "{" + inner + "}"

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Registry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._families: dict[str, _Family] = {}

    # -- family registration -------------------------------------------------

    def counter(self, name: str, help_text: str, labels: tuple[str, ...] = (),
                cap: int = 256) -> _Family:
        with self._lock:
            if name not in self._families:
                self._families[name] = _Family(name, help_text, "counter", labels, cap)
            return self._families[name]

    def gauge(self, name: str, help_text: str, labels: tuple[str, ...] = (),
              cap: int = 256) -> _Family:
        with self._lock:
            if name not in self._families:
                self._families[name] = _Family(name, help_text, "gauge", labels, cap)
            return self._families[name]

    # -- rendering -------------------------------------------------------------

    def render_prometheus(self) -> str:
        with self._lock:
            lines: list[str] = []
            for family in self._families.values():
                lines.extend(family.render())
            return "\n".join(lines) + "\n"


#: The process-wide registry (one per worker process).
registry = _Registry()

# -- declared families (stable names; bounded label domains) --------------------

jobs_claimed_total = registry.counter(
    "clipforge_jobs_claimed_total", "Jobs claimed by stage.", ("stage",))
queue_wait_seconds_sum = registry.counter(
    "clipforge_queue_wait_seconds_sum", "Seconds jobs waited in the queue before their FIRST claim (sum).", ("stage",))
queue_wait_seconds_count = registry.counter(
    "clipforge_queue_wait_seconds_count", "First-claim queue wait observations (count).", ("stage",))
job_processing_seconds_sum = registry.counter(
    "clipforge_job_processing_seconds_sum", "Wall-clock seconds spent processing one claim, by stage (sum).", ("stage",))
job_processing_seconds_count = registry.counter(
    "clipforge_job_processing_seconds_count", "Claim processing observations (count).", ("stage",))
jobs_completed_total = registry.counter(
    "clipforge_jobs_completed_total", "Jobs completed, by stage.", ("stage",))
jobs_failed_total = registry.counter(
    "clipforge_jobs_failed_total", "Job failure writes, by stage and error category.", ("stage", "error_category"))
jobs_released_total = registry.counter(
    "clipforge_jobs_released_total", "Jobs released back to a stage entry point (no failure).", ("stage",))
jobs_cancelled_total = registry.counter(
    "clipforge_job_cancellations_total", "Job processing tasks cancelled (lease loss / shutdown).", ("stage",))
lease_renewals_failed_total = registry.counter(
    "clipforge_lease_renewals_failed_total", "Lease renewals rejected (ownership lost).")
storage_failures_total = registry.counter(
    "clipforge_storage_failures_total", "Storage operations that failed.", ("operation",))
claim_errors_total = registry.counter(
    "clipforge_claim_errors_total", "Queue claim transport errors (backoff engaged).")
provider_validations_total = registry.counter(
    "clipforge_provider_validations_total", "Provider URL validations by outcome.",
    ("provider", "outcome"))

worker_active_jobs = registry.gauge("clipforge_worker_active_jobs", "Currently active job tasks.")
worker_uptime_seconds = registry.gauge("clipforge_worker_uptime_seconds", "Worker uptime (seconds).")
worker_consecutive_claim_errors = registry.gauge(
    "clipforge_worker_consecutive_claim_errors", "Consecutive claim transport errors (backoff state).")

# -- bounded label helpers -------------------------------------------------------

#: The closed set of queue error categories (the ONLY values ever used for
#: the ``error_category`` label; anything else maps to 'other').
_ERROR_CATEGORIES: tuple[str, ...] = ()  # populated lazily (see _error_category)


def _error_category(category: str) -> str:
    global _ERROR_CATEGORIES
    if not _ERROR_CATEGORIES:
        from ..queue import models as queue_models  # leaf module; no cycle

        values = {
            value for name, value in vars(queue_models).items()
            if name.startswith("ERROR_") and isinstance(value, str)
        }
        _ERROR_CATEGORIES = tuple(sorted(values))
    return _normalize(category, _ERROR_CATEGORIES)


# -- ergonomic observers -----------------------------------------------------------

def observe_queue_wait(stage: str, wait_seconds: float) -> None:
    if wait_seconds is None or wait_seconds < 0:
        return
    queue_wait_seconds_sum.inc({"stage": stage}, wait_seconds)
    queue_wait_seconds_count.inc({"stage": stage})


def observe_processing_time(stage: str, seconds: float) -> None:
    if seconds < 0:
        return
    job_processing_seconds_sum.inc({"stage": stage}, seconds)
    job_processing_seconds_count.inc({"stage": stage})


def observe_provider_validation(provider: str, outcome: str) -> None:
    provider_validations_total.inc({
        "provider": _normalize(provider, PROVIDERS),
        "outcome": _normalize(outcome, PROVIDER_OUTCOMES),
    })


def observe_storage_failure(operation: str) -> None:
    storage_failures_total.inc({"operation": _normalize(operation, STORAGE_OPERATIONS)})


def gauge_active_jobs(value: int) -> None:
    worker_active_jobs.set(max(0, int(value)))


def gauge_uptime(seconds: float) -> None:
    worker_uptime_seconds.set(max(0.0, float(seconds)))


def gauge_claim_errors(value: int) -> None:
    worker_consecutive_claim_errors.set(max(0, int(value)))


__all__ = [
    "registry",
    "jobs_claimed_total", "jobs_completed_total", "jobs_failed_total",
    "jobs_released_total", "jobs_cancelled_total", "lease_renewals_failed_total",
    "storage_failures_total", "claim_errors_total", "provider_validations_total",
    "worker_active_jobs", "worker_uptime_seconds", "worker_consecutive_claim_errors",
    "observe_queue_wait", "observe_processing_time", "observe_provider_validation",
    "observe_storage_failure", "gauge_active_jobs", "gauge_uptime", "gauge_claim_errors",
    "stage_for_status", "_error_category",
    "STAGES", "PROVIDERS", "PROVIDER_OUTCOMES", "STORAGE_OPERATIONS",
]
