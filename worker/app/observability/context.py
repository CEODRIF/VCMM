"""Per-job structured log context (TASK 012, spec section 6).

A claimed job is processed in an asyncio task; contextvars give every
log line emitted inside that task (including from the queue client,
storage, subprocesses' callers, and provider layer) the same correlation
fields without threading parameters through every function:

- ``job_id`` — the jobs row being processed;
- ``worker_id`` — the owning worker instance;
- ``correlation_id`` — a short id unique to THIS claim/processing run
  (a job claimed three times has three correlation ids);
- ``stage`` — validating | analysis | rendering (bounded);
- ``attempt`` — the job's attempt_count at claim time;
- ``error_code`` — set by the processor right before a failure write.

``duration_ms`` is NOT a contextvar: it is a per-log fact, passed via
``logger.info(..., extra={"duration_ms": ...})`` and merged by the JSON
formatter when present.
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional

job_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("cf_job_id", default=None)
worker_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("cf_worker_id", default=None)
correlation_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("cf_correlation_id", default=None)
stage_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("cf_stage", default=None)
attempt_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("cf_attempt", default=None)
error_code_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("cf_error_code", default=None)
provider_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("cf_provider", default=None)

#: The whitelisted ``extra`` keys the JSON formatter merges into the
#: payload (bounded; unknown extras are ignored, never leaked).
EXTRA_FIELDS = ("duration_ms", "bytes", "object_count")


@contextmanager
def job_context(
    *,
    job_id: str,
    worker_id: str,
    stage: str,
    attempt: int | None = None,
    provider: str | None = None,
) -> Iterator[str]:
    """Binds the job-scoped log context for the duration of one claim.

    Yields the ``correlation_id`` for this run.
    """
    correlation_id = uuid.uuid4().hex[:16]
    tokens = [
        job_id_var.set(str(job_id)),
        worker_id_var.set(str(worker_id)),
        correlation_id_var.set(correlation_id),
        stage_var.set(str(stage) or None),
        attempt_var.set(int(attempt) if attempt is not None else None),
        provider_var.set(str(provider) if provider else None),
    ]
    try:
        yield correlation_id
    finally:
        for token, var in zip(tokens, (job_id_var, worker_id_var, correlation_id_var,
                                       stage_var, attempt_var, provider_var)):
            var.reset(token)


def set_error_code(error_code: str) -> None:
    """Called by processors right before a failure write so the surrounding
    log lines carry the typed error category."""
    error_code_var.set(str(error_code or "") or None)
