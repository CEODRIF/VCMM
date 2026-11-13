"""Structured (JSON) logging for the worker.

Every log line is a single JSON object on stdout so log aggregators can
index level, logger, message and exceptions without parsing prose.

TASK 012 (spec section 6) adds:

- JOB CONTEXT: lines emitted inside a claimed job's task automatically
  carry ``job_id``, ``worker_id``, ``correlation_id``, ``stage``,
  ``attempt`` and (when set) ``error_code``/``provider`` — via the
  contextvars in ``app.observability.context``;
- whitelisted per-log facts (``duration_ms``, ``bytes``, ``object_count``)
  merged from ``logger.info(..., extra={...})``;
- SECRET REDACTION: every message and traceback is passed through the
  provider-layer ``redact()`` (authorization/bearer, tokens, secrets,
  cookies, signed-URL signatures) BEFORE it can reach stdout. A bug that
  accidentally logs a credential cannot leak it.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from ..observability import context as log_context
from ..providers.errors import redact

_CONTEXT_FIELDS = (
    ("job_id", log_context.job_id_var),
    ("worker_id", log_context.worker_id_var),
    ("correlation_id", log_context.correlation_id_var),
    ("stage", log_context.stage_var),
    ("attempt", log_context.attempt_var),
    ("error_code", log_context.error_code_var),
    ("provider", log_context.provider_var),
)


class JsonFormatter(logging.Formatter):
    """Formats a log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()) or "",
        }
        # Job-scoped context (only the fields that are bound).
        for name, var in _CONTEXT_FIELDS:
            value = var.get()
            if value is not None:
                payload[name] = value
        # Whitelisted per-log facts passed via ``extra=``.
        for name in log_context.EXTRA_FIELDS:
            value = record.__dict__.get(name)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exc_info"] = redact(self.formatException(record.exc_info)) or ""
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Replace the root logger handlers with a single JSON stdout handler."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
