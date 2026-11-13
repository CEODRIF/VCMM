"""Job queue: models + Supabase-backed queue (Phase 3/4).

All queue control goes through restricted security-definer RPCs; Phase 4
adds two guarded direct updates (stage transition + analysis parking)
scoped to the owning worker.
"""
