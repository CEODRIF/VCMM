"""Service-role, job-scoped DB access (worker-side).

Mirrors the safety contract of :class:`app.intel.checkpoints.IntelStore`
and :class:`app.storage.clip_store.SupabaseClipStore`: UUID-validated,
job-scoped queries only — a stale run can never widen its scope to
another job's rows.
"""
