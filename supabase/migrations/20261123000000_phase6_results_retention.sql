-- =============================================================================
-- ClipForge AI — Phase 6: artifact retention markers (results + cleanup)
-- =============================================================================
-- Explicit retention policy (designed BEFORE any deletion):
--
--   * When a job reaches a TERMINAL state (`completed` or `failed`), its
--     artifacts (the source object and every rendered clip object) become
--     eligible for cleanup 30 days later. The deadline is written to
--     `jobs.expires_at` (column + index already exist since Phase 1-2 —
--     previously never set) by a trigger, so the marker is DATABASE-BACKED
--     and visible to the API/UI — not a guessed period evaluated at delete
--     time.
--   * `jobs.purged_at` (new, nullable) is the IDEMPOTENCY marker for the
--     cleanup: the purge routine only touches jobs with
--     `expires_at < now() AND purged_at IS NULL AND status IN
--     ('completed','failed')`, and sets `purged_at` only AFTER the storage
--     deletions succeed — a crash between the two simply makes the next run
--     repeat no-op deletions and finish (at-least-once, never re-armed).
--   * NOTHING in this migration deletes anything. The purge itself runs in
--     the worker service (python -m app.cleanup, manual for now — no
--     automatic scheduler is wired until the policy is approved in
--     production). Active/parked jobs are never touched.
--
-- Changing the policy: the 30-day interval lives in this migration's
-- trigger function (ALTER it to change). It is applied only when
-- `expires_at` is still NULL, so an operator can pre-set a different
-- deadline per job and it will never be overridden.
--
-- Non-destructive and additive: one nullable column, one trigger, no
-- changes to existing rows, RPCs, grants or RLS policies.
-- Applies AFTER 20261120000000. Executed + asserted against a real
-- PostgreSQL engine in the test suite (tests/database/phase6-retention.test.ts).
-- Never applied to production.
-- =============================================================================

alter table public.jobs
    add column if not exists purged_at timestamptz;

comment on column public.jobs.purged_at is
    'Set by the (manual, idempotent) artifact purge once the job''s source + clip objects were deleted; the purge only runs when expires_at < now() AND purged_at IS NULL AND the job is terminal.';

create or replace function public.apply_job_retention_policy()
returns trigger
language plpgsql
as $$
begin
    if (new.status = 'completed' or new.status = 'failed')
       and old.status is distinct from new.status
       and new.expires_at is null
    then
        new.expires_at := now() + interval '30 days';
    end if;
    return new;
end;
$$;

comment on function public.apply_job_retention_policy() is
    'Phase 6 retention policy: a job reaching a terminal state (completed or failed) gets expires_at = now() + 30 days, only when no deadline was set already.';

drop trigger if exists jobs_apply_retention on public.jobs;
create trigger jobs_apply_retention
    before update on public.jobs
    for each row
    execute function public.apply_job_retention_policy();
