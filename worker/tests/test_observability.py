"""TASK 012 observability tests: metrics registry, bounded cardinality,
structured JSON log schema, job-context correlation, secret redaction.

All assertions are deterministic — no network, no timing, no randomness.
"""

from __future__ import annotations

import io
import json
import logging
import re

from app.config.logging import JsonFormatter
from app.observability import context as ctx
from app.observability import metrics as m
from app.providers.errors import redact

HEX16 = re.compile(r"^[0-9a-f]{16}$")


# -- stage mapping (bounded domain) ------------------------------------------------

def test_stage_for_status_maps_known_statuses():
    assert m.stage_for_status("validating") == "validating"
    assert m.stage_for_status("transcribing") == "analysis"
    assert m.stage_for_status("analyzing") == "analysis"
    assert m.stage_for_status("rendering") == "rendering"


def test_stage_for_status_unknown_never_grows_cardinality():
    # Unknown/empty statuses must map to the closed fallback, never pass
    # through raw (a raw pass-through would be an unbounded label domain).
    for junk in ("", None, "completed", "failed", "queued", "x" * 500, "\n"):
        assert m.stage_for_status(str(junk or "")) == "other"


# -- registry: counter / gauge semantics -------------------------------------------

def test_counter_accumulates_and_gauge_replaces():
    reg = m._Registry()
    c = reg.counter("t_counter", "help", ("k",))
    g = reg.gauge("t_gauge", "help", ("k",))
    c.inc({"k": "a"})
    c.inc({"k": "a"})
    c.inc({"k": "a"}, amount=2.5)
    c.inc({"k": "b"}, 0.5)
    assert c.values[("a",)] == 4.5
    assert c.values[("b",)] == 0.5
    g.set(7, {"k": "a"})
    g.set(2, {"k": "a"})
    assert g.values[("a",)] == 2
    with __import__("pytest").raises(ValueError):
        c.set(1)  # set() on a counter is a type error
    with __import__("pytest").raises(ValueError):
        g.inc()  # inc() on a gauge is a type error


def test_unknown_label_names_are_rejected_not_grown():
    reg = m._Registry()
    c = reg.counter("t_unknown_labels", "help", ("stage",))
    c.inc({"stage": "validating"})
    c.inc({"bogus": "nope"})  # unknown label: dropped, no new series
    c.inc(None)  # no labels -> key ("",) for the declared set
    assert ("validating",) in c.values
    assert ("nope",) not in c.values
    # 'bogus' never created a series with that value.
    assert all(v != "nope" for k in c.values for v in k)


def test_series_cap_bounds_cardinality():
    reg = m._Registry()
    c = reg.counter("capped", "help", ("k",), cap=4)
    for i in range(10):
        c.inc({"k": f"v{i}"})
    assert len(c.values) == 4  # hard cap: the first 4, then drops
    assert set(c.values) == {("v0",), ("v1",), ("v2",), ("v3",)}
    # The dropped values never appear.
    for i in range(4, 10):
        assert (f"v{i}",) not in c.values


def test_prometheus_render_format_and_escaping():
    reg = m._Registry()
    c = reg.counter("esc_counter", "An escaped counter.", ("k",))
    c.inc({"k": 'we"ird\nval\\ue'}, 1.25)
    g = reg.gauge("empty_gauge", "Unused gauge.")
    out = reg.render_prometheus()
    lines = out.splitlines()
    assert out.endswith("\n")
    assert "# TYPE esc_counter counter" in lines
    assert "# HELP esc_counter An escaped counter." in lines
    assert "# TYPE empty_gauge gauge" in lines
    # Empty family still renders one visible zero series.
    assert "empty_gauge 0" in lines
    # Label escaping: backslash, quote, newline.
    esc_line = next(ln for ln in lines if ln.startswith("esc_counter{"))
    assert 'k="we\\"ird\\nval\\\\ue"' in esc_line
    assert esc_line.endswith("1.25")


# -- error-category normalization ----------------------------------------------------

def test_error_category_is_bounded_to_known_queue_codes():
    from app.queue import models as qm

    # A real code maps to itself...
    real = qm.ERROR_PROVIDER_INVALID_URL
    assert m._error_category(real) == real
    # ...anything else (including a long attacker-chosen string) -> 'other'.
    assert m._error_category("not-a-real-code") == "other"
    assert m._error_category("x" * 200) == "other"
    assert m._error_category(None) == "other"


def test_error_category_values_are_a_closed_set():
    from app.queue import models as qm

    expected = {
        v for n, v in vars(qm).items()
        if n.startswith("ERROR_") and isinstance(v, str)
    }
    assert m._error_category  # populated lazily
    assert set(m._ERROR_CATEGORIES or tuple()) in (set(), expected) or True
    # After one call the lazy set is populated and equals the models' codes.
    m._error_category(qm.ERROR_PROVIDER_INVALID_URL)
    assert set(m._ERROR_CATEGORIES) == expected


# -- observers: delta-based on the global registry -----------------------------------

def _family_value(family, key) -> float:
    return family.values.get(key, 0.0)


def test_observe_queue_wait_records_sum_and_count():
    before_sum = _family_value(m.queue_wait_seconds_sum, ("validating",))
    before_count = _family_value(m.queue_wait_seconds_count, ("validating",))
    m.observe_queue_wait("validating", 12.5)
    assert _family_value(m.queue_wait_seconds_sum, ("validating",)) == before_sum + 12.5
    assert _family_value(m.queue_wait_seconds_count, ("validating",)) == before_count + 1
    # Negative / None waits are ignored (not an error).
    m.observe_queue_wait("validating", -1)
    m.observe_queue_wait("validating", None)
    assert _family_value(m.queue_wait_seconds_count, ("validating",)) == before_count + 1


def test_observe_processing_time_records_sum_and_count():
    before_sum = _family_value(m.job_processing_seconds_sum, ("rendering",))
    before_count = _family_value(m.job_processing_seconds_count, ("rendering",))
    m.observe_processing_time("rendering", 3.25)
    assert _family_value(m.job_processing_seconds_sum, ("rendering",)) == before_sum + 3.25
    assert _family_value(m.job_processing_seconds_count, ("rendering",)) == before_count + 1
    m.observe_processing_time("rendering", -5)  # ignored
    assert _family_value(m.job_processing_seconds_count, ("rendering",)) == before_count + 1


def test_observe_provider_validation_normalizes_out_of_domain_values():
    before_ok = _family_value(m.provider_validations_total, ("youtube", "available"))
    before_other_p = _family_value(m.provider_validations_total, ("other", "other"))
    m.observe_provider_validation("youtube", "available")
    m.observe_provider_validation("tiktok", "exploded")  # out of domain -> other/other
    assert _family_value(m.provider_validations_total, ("youtube", "available")) == before_ok + 1
    assert _family_value(m.provider_validations_total, ("other", "other")) == before_other_p + 1
    assert ("tiktok", "exploded") not in m.provider_validations_total.values


def test_observe_storage_failure_normalizes_operation():
    before = _family_value(m.storage_failures_total, ("download",))
    m.observe_storage_failure("download")
    m.observe_storage_failure("rm_rf")  # out of domain -> other
    assert _family_value(m.storage_failures_total, ("download",)) == before + 1
    assert ("rm_rf",) not in m.storage_failures_total.values


def test_global_registry_renders_every_declared_family():
    out = m.registry.render_prometheus()
    for name in (
        "clipforge_jobs_claimed_total",
        "clipforge_jobs_completed_total",
        "clipforge_jobs_failed_total",
        "clipforge_jobs_released_total",
        "clipforge_job_cancellations_total",
        "clipforge_lease_renewals_failed_total",
        "clipforge_storage_failures_total",
        "clipforge_claim_errors_total",
        "clipforge_provider_validations_total",
        "clipforge_queue_wait_seconds_sum",
        "clipforge_job_processing_seconds_sum",
        "clipforge_worker_active_jobs",
        "clipforge_worker_uptime_seconds",
        "clipforge_worker_consecutive_claim_errors",
    ):
        assert f"# TYPE {name} " in out, f"missing family {name}"


# -- job context (correlation) -------------------------------------------------------

def test_job_context_binds_and_clears_fields():
    assert ctx.job_id_var.get() is None
    with ctx.job_context(job_id="j1", worker_id="w1", stage="analysis", attempt=2) as corr:
        assert HEX16.match(corr)
        assert ctx.job_id_var.get() == "j1"
        assert ctx.worker_id_var.get() == "w1"
        assert ctx.correlation_id_var.get() == corr
        assert ctx.stage_var.get() == "analysis"
        assert ctx.attempt_var.get() == 2
        assert ctx.provider_var.get() is None
    # Fully cleared afterwards.
    assert ctx.job_id_var.get() is None
    assert ctx.worker_id_var.get() is None
    assert ctx.correlation_id_var.get() is None
    assert ctx.stage_var.get() is None
    assert ctx.attempt_var.get() is None


def test_job_context_correlation_ids_are_unique_per_claim():
    with ctx.job_context(job_id="j1", worker_id="w1", stage="validating") as c1:
        pass
    with ctx.job_context(job_id="j1", worker_id="w1", stage="validating") as c2:
        pass
    assert c1 != c2  # two claims of the same job = two correlation ids


def test_job_context_nesting_restores_outer():
    with ctx.job_context(job_id="outer", worker_id="w", stage="analysis") as outer_corr:
        with ctx.job_context(job_id="inner", worker_id="w", stage="rendering") as inner_corr:
            assert ctx.correlation_id_var.get() == inner_corr
            assert ctx.job_id_var.get() == "inner"
        assert ctx.correlation_id_var.get() == outer_corr
        assert ctx.job_id_var.get() == "outer"
        assert ctx.stage_var.get() == "analysis"
    assert ctx.job_id_var.get() is None


def test_set_error_code_binds_and_accepts_empty():
    with ctx.job_context(job_id="j", worker_id="w", stage="validating"):
        ctx.set_error_code("provider_invalid_url")
        assert ctx.error_code_var.get() == "provider_invalid_url"
        ctx.set_error_code("")
        assert ctx.error_code_var.get() is None
    assert ctx.error_code_var.get() is None


# -- JSON log schema -------------------------------------------------------------------

class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.buffer = io.StringIO()
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.buffer.write(self.format(record) + "\n")

    def lines(self) -> list[dict]:
        return [json.loads(ln) for ln in self.buffer.getvalue().splitlines() if ln]


def _logger_with_capture(name: str) -> tuple[logging.Logger, _CaptureHandler]:
    logger = logging.getLogger(name)
    handler = _CaptureHandler()
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, handler


def test_log_schema_without_context_has_core_fields_only():
    logger, handler = _logger_with_capture("t.obs.noctx")
    logger.info("hello world")
    payload = handler.lines()[0]
    assert set(payload) == {"timestamp", "level", "logger", "message"}
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", payload["timestamp"])


def test_log_schema_inside_job_context_carries_correlation_fields():
    logger, handler = _logger_with_capture("t.obs.ctx")
    with ctx.job_context(job_id="job-123", worker_id="w-9", stage="analysis",
                         attempt=3, provider="youtube") as corr:
        logger.info("processing", extra={"duration_ms": 42})
    payload = handler.lines()[0]
    assert payload["job_id"] == "job-123"
    assert payload["worker_id"] == "w-9"
    assert payload["correlation_id"] == corr
    assert payload["stage"] == "analysis"
    assert payload["attempt"] == 3
    assert payload["provider"] == "youtube"
    assert payload["duration_ms"] == 42
    # No error code was set -> field absent, not null.
    assert "error_code" not in payload


def test_whitelisted_extras_merged_unknown_extras_ignored():
    logger, handler = _logger_with_capture("t.obs.extra")
    logger.info("with facts", extra={"duration_ms": 1, "bytes": 2048,
                                     "object_count": 3, "user_secret": "x"})
    payload = handler.lines()[0]
    assert payload["duration_ms"] == 1
    assert payload["bytes"] == 2048
    assert payload["object_count"] == 3
    assert "user_secret" not in payload  # unknown extra never leaks


def test_log_redaction_masks_credentials_in_message():
    logger, handler = _logger_with_capture("t.obs.redact")
    jwt = "eyJhbGciOiJIUzI1NiJ9.c2VjcmV0LXBheWxvYWQ.sig123"
    logger.info("auth header was Authorization: Bearer %s", jwt)
    payload = handler.lines()[0]
    assert jwt not in json.dumps(payload)
    assert "Authorization: ***" in payload["message"]


def test_log_redaction_masks_tracebacks():
    logger, handler = _logger_with_capture("t.obs.trace")
    try:
        raise RuntimeError("password=hunter2secret99 leaked in traceback")
    except RuntimeError:
        logger.exception("boom")
    payload = handler.lines()[0]
    assert "exc_info" in payload
    assert "hunter2secret99" not in json.dumps(payload)
    assert "password=***" in payload["exc_info"]


# -- redact() unit behavior -------------------------------------------------------------

def test_redact_none_passthrough():
    assert redact(None) is None


def test_redact_masks_all_credential_shapes():
    long_hex = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    masked = [
        redact(f"Authorization: Bearer {long_hex}"),
        redact(f"Bearer {long_hex}"),
        redact(f"access_token={long_hex}"),
        redact(f'refresh_token="{long_hex}"'),
        redact(f"client_secret={long_hex}"),
        redact(f"api_key: {long_hex}"),
        redact(f"Cookie: session={long_hex}"),
    ]
    for out in masked:
        assert long_hex not in out, f"secret leaked through redaction: {out!r}"
    # The masked output keeps the key name (diagnosable) but not the value.
    assert redact(f"api_key: {long_hex}") == "api_key: ***"
    # Headers are masked to end-of-line, so a multi-word value can't survive.
    assert redact(f"Authorization: Bearer {long_hex}") == "Authorization: ***"
    assert redact(f"Cookie: session={long_hex}") == "Cookie: ***"


def test_redact_masks_signed_storage_urls_including_short_tokens():
    url_long = "https://x.supabase.co/storage/v1/object/sign/clips/j/c.mp4?token=eyJhbGci.abcdef1234567890"
    out = redact(url_long)
    assert "abcdef1234567890" not in out
    assert out == url_long.split("token=")[0] + "token=***"
    # SHORT tokens (below the key=value pattern's 8-char gate) are masked too.
    url_short = "https://x.supabase.co/storage/v1/object/sign/foo.mp4?token=123.456"
    assert redact(url_short) == url_short.split("token=")[0] + "token=***"


def test_redact_masks_bare_token_values_and_never_crashes():
    # The bare-token pattern must be self-contained (own group) — it must
    # not raise even when it is the only pattern that matches.
    out = redact("got token=" + "a" * 20)
    assert "a" * 20 not in out
    # Short bare tokens below every pattern's gate are data, not secrets —
    # left as-is (no crash).
    assert redact("token=abc") == "token=abc"


def test_redact_leaves_benign_text_alone():
    benign = "job job-1 claimed, stage=validating, 3 clips, duration_ms=1200"
    assert redact(benign) == benign
