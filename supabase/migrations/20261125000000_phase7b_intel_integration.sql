-- =============================================================================
-- ClipForge AI — Phase 7b (TASK 007): stream-intelligence INTEGRATION
-- =============================================================================
-- The Phase 7 foundation (20261124000000) delivered a complete, resumable,
-- chunked analysis pipeline (`StreamIntelPipeline`) — but it was deliberately
-- NOT wired into the worker job lifecycle (docs/stream-intelligence.md §1.3 /
-- §11). This migration supplies the ONE missing queue-side piece that the
-- integration needs: a way for a job whose analysis is INCOMPLETE (its
-- per-claim chunk budget was exhausted) to return to a claimable state and
-- resume from its checkpoints — WITHOUT being confused with a failure retry
-- and WITHOUT being claimed by the Phase 5A renderer.
--
-- Design follows the codebase's own precedent for "a stage that must not be
-- starved by the shared failure budget" (Phase 5, migration 20261117000000,
-- `render_attempts`): a dedicated per-stage counter.
--
-- Non-destructive and additive:
--   * adds ONE column to `jobs` (`analysis_resumes`, default 0) — a dedicated
--     counter of incomplete-analysis resumes;
--   * adds ONE new RPC (`requeue_incomplete_analysis`) — the only path that
--     returns an owned active analysis job to `ready` as a resume;
--   * re-creates the EXISTING `claim_next_job` (same signature — backward
--     compatible) with ONE added, mutually-exclusive claim branch for a
--     `ready` job that is mid-resume, and makes such a resume claim NOT
--     consume the shared `attempt_count` failure budget;
--   * NO existing rows, clips, grants, RLS policies, other RPCs or the
--     transition map are modified. Every status transition this flow uses
--     (transcribing→ready, analyzing→ready, ready→transcribing) is ALREADY
--     legal per the Phase 4/5 transition map.
--
-- Why a dedicated resume counter (and not attempt_count):
--   `attempt_count` is consumed by EVERY ordinary claim and is the safety
--   valve that turns repeated failures into `failed` (default max_attempts=3).
--   A long recording legitimately needs MANY claims to process all of its
--   chunks (a 100 h VOD at 600 s chunks / 20 per claim ≈ 30 claims). Counting
--   those against the failure budget would strand the job after 3 claims —
--   exactly the "parked right after using its budget → stranded forever"
--   problem Phase 5 solved for rendering with `render_attempts`. A resume is
--   PROGRESS, not a failure, so it has its own counter and its own (generous,
--   configurable) cap that a legitimate VOD will never hit but a genuinely
--   stuck pipeline eventually will.
--
-- Application: run in a VERIFIED development Supabase project AFTER the
-- Phase 7 migration (supabase dashboard → SQL editor, or the CLI). NEVER
-- applied to production. Executed + asserted against a real PostgreSQL engine
-- in the test suite (tests/database/phase7b-integration.test.ts).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Dedicated analysis-resume counter
-- -----------------------------------------------------------------------------
alter table public.jobs
    add column if not exists analysis_resumes integer not null default 0
    check (analysis_resumes >= 0);

comment on column public.jobs.analysis_resumes is
    'Incomplete-analysis resumes for this job (Phase 7b). A job that returned "incomplete" (chunk budget exhausted) is re-queued to ready with this counter incremented and is re-claimed by the resume branch of claim_next_job without consuming the failure attempt budget; it stops only when the pipeline completes or the (configurable) resume cap is exhausted.';

-- -----------------------------------------------------------------------------
-- 2. requeue_incomplete_analysis — the "incomplete, continue later" transition
-- -----------------------------------------------------------------------------
-- Returns ONE owned, ACTIVE analysis job (status transcribing or analyzing,
-- locked by the caller) back to `ready` so the claim RPC re-enters it. This is
-- DISTINCT from:
--   * park_after_analysis  — Phase 4 "done, awaiting rendering" (lock cleared,
--     NO status change; the Phase 5A renderer claims it);
--   * release_job          — generic graceful release to a stage entry point;
--   * fail_job             — a FAILURE (consumes attempt budget, requeues to
--     queued/ready, eventually failed).
-- A resume does NOT consume `attempt_count` (it is progress, not a failure).
-- Returns:
--   'ready'            — re-queued for resume (analysis_resumes incremented);
--   'resume_exhausted' — the resume cap was reached; the caller must record a
--                        failure (this RPC performs NO terminal write itself,
--                        preserving the single-terminal-write contract);
--   'not_owner'        — the caller no longer owns the job (no state written).
create or replace function public.requeue_incomplete_analysis(
    p_job_id uuid,
    p_worker_id text,
    p_max_resumes integer default 100000
)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
    job public.jobs;
begin
    if p_worker_id is null or length(p_worker_id) = 0 then
        raise exception 'invalid worker id';
    end if;
    if p_max_resumes is null or p_max_resumes < 1 then
        raise exception 'max resumes must be >= 1';
    end if;

    select j.* into job from public.jobs j
     where j.id = p_job_id
       and j.locked_by = p_worker_id
       and j.status in ('transcribing', 'analyzing')
     for update;

    if not found then
        return 'not_owner';
    end if;

    if job.analysis_resumes + 1 >= p_max_resumes then
        -- The pipeline has been resumed to the cap without completing: a
        -- genuinely stuck run. Do NOT write a terminal state here — the caller
        -- records the failure via fail_job (single terminal write).
        return 'resume_exhausted';
    end if;

    -- Graceful resume: back to the analysis entry point. transcribing→ready and
    -- analyzing→ready are both legal (Phase 4). The lock is cleared, so the job
    -- is a plain claimable `ready` row — and, with analysis_resumes > 0, it is
    -- recognised as a RESUME (not a fresh analysis or a failure retry).
    update public.jobs
       set status = 'ready',
           analysis_resumes = job.analysis_resumes + 1,
           last_error = null,
           locked_by = null,
           locked_at = null,
           lease_expires_at = null
     where id = job.id;

    return 'ready';
end;
$$;

-- -----------------------------------------------------------------------------
-- 3. claim_next_job — add the mid-resume claim branch (signature unchanged)
-- -----------------------------------------------------------------------------
-- Claimable (mutually exclusive branches, FIFO by created_at):
--   1. queued / ready          (attempt budget available)  — Phases 3-5
--   2. ready + analysis_resumes > 0  (mid-intel-resume)    — Phase 7b:
--      a job making chunking progress is claimable regardless of the failure
--      attempt budget (the cap is enforced by requeue_incomplete_analysis).
--   3. analyzing + lock CLEARED (PARKED; render budget)    → rendering — Phase 5
--   4. any active stage with an EXPIRED lease (crash recovery, stage kept)
-- A resume claim (branch 2) must NOT increment attempt_count: a long VOD needs
-- many resume claims and must not be starved by the failure budget.
create or replace function public.claim_next_job(
    p_worker_id text,
    p_lease_seconds integer
)
returns public.jobs
language plpgsql
security definer
set search_path = public
as $$
declare
    job public.jobs;
begin
    if p_worker_id is null or length(p_worker_id) = 0 or length(p_worker_id) > 128 then
        raise exception 'invalid worker id';
    end if;
    if p_lease_seconds is null or p_lease_seconds < 30 or p_lease_seconds > 3600 then
        raise exception 'lease seconds must be between 30 and 3600';
    end if;

    select j.*
      into job
      from public.jobs j
     where (
               (j.status in ('queued', 'ready')
                    and j.attempt_count < j.max_attempts)
            or (j.status = 'ready' and j.analysis_resumes > 0)
            or (j.status = 'analyzing' and j.locked_by is null
                    and j.render_attempts < j.max_attempts)
            or (j.status in ('validating', 'transcribing', 'analyzing', 'rendering')
                    and j.lease_expires_at is not null and j.lease_expires_at < now()
                    and j.attempt_count < j.max_attempts)
           )
     order by j.created_at
     limit 1
     for update skip locked;

    if not found then
        return null;
    end if;

    update public.jobs
       set status = case
               when job.status = 'queued' then 'validating'
               when job.status = 'ready' then 'transcribing'
               when job.status = 'analyzing' and job.locked_by is null then 'rendering'
               else job.status
           end,
           attempt_count = case
               -- A mid-intel-resume claim is PROGRESS, not a failure retry:
               -- it must not consume the shared failure attempt budget.
               when job.status = 'ready' and job.analysis_resumes > 0
                   then job.attempt_count
               else job.attempt_count + 1
           end,
           locked_by = p_worker_id,
           locked_at = now(),
           lease_expires_at = now() + make_interval(secs => p_lease_seconds)
     where id = job.id
     returning * into job;

    return job;
end;
$$;

-- -----------------------------------------------------------------------------
-- 4. Privileges: service-role-only for ALL queue-control RPCs (incl. the new
--    requeue_incomplete_analysis). Defense in depth after the replace.
-- -----------------------------------------------------------------------------
revoke execute on function
    public.claim_next_job(text, integer),
    public.renew_job_lease(uuid, text, integer),
    public.update_job_progress(uuid, text, integer),
    public.complete_job_ingestion(uuid, text, jsonb),
    public.complete_job_rendering(uuid, text),
    public.requeue_incomplete_analysis(uuid, text, integer),
    public.fail_job(uuid, text, text, text, boolean),
    public.release_job(uuid, text)
    from public;

grant execute on function
    public.claim_next_job(text, integer),
    public.renew_job_lease(uuid, text, integer),
    public.update_job_progress(uuid, text, integer),
    public.complete_job_ingestion(uuid, text, jsonb),
    public.complete_job_rendering(uuid, text),
    public.requeue_incomplete_analysis(uuid, text, integer),
    public.fail_job(uuid, text, text, text, boolean),
    public.release_job(uuid, text)
    to service_role;

comment on function public.requeue_incomplete_analysis(uuid, text, integer) is
    'Returns an owned active analysis job (transcribing/analyzing) to ready as an INCOMPLETE-ANALYSIS resume (analysis_resumes + 1, attempt_count untouched). Distinct from park (done, awaiting render), release (stage entry point) and fail (a failure). Returns ready | resume_exhausted | not_owner.';

comment on function public.claim_next_job(text, integer) is
    'Atomically claims the oldest claimable job: queued/ready (attempt budget), a ready job mid-intel-resume (analysis_resumes > 0, does NOT consume attempt_count), PARKED analyzing (render budget → rendering), or an active stage with an expired lease (crash recovery) via FOR UPDATE SKIP LOCKED.';
