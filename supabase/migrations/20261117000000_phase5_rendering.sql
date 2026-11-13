-- =============================================================================
-- ClipForge AI — Phase 5: rendering stage (queue + terminal completion)
-- =============================================================================
-- Non-destructive and additive:
--   * adds ONE column to `jobs` (`render_attempts`, default 0) — a dedicated
--     retry budget for the rendering stage,
--   * extends the lock-consistency constraint so an ACTIVE rendering job
--     holds its lease (a parked analyzing job stays legal as before),
--   * adds ONE transition (rendering → analyzing = rendering retry to the
--     parked state, where the claim picks it up again),
--   * extends the EXISTING queue RPCs with the `rendering` stage (same
--     signatures — backward compatible),
--   * adds ONE new RPC (`complete_job_rendering`) that is the ONLY way a job
--     can become `completed` during rendering: it requires ownership AND at
--     least one clip AND every clip `ready` WITH a `storage_path` — the
--     terminal state can never be claimed falsely.
-- No existing rows, clips, jobs, grants or RLS policies are modified.
--
-- Applies AFTER 20261116000000 on the same verified development project.
-- Executed + asserted against a real PostgreSQL engine in the test suite
-- (tests/database/phase5-rendering.test.ts). Never applied to production.
-- =============================================================================

-- 1. Dedicated rendering retry budget ------------------------------------------
-- `attempt_count` is consumed by EVERY claim (ingestion, analysis, recovery),
-- so a job parked right after using its budget would be stranded forever,
-- while ignoring the budget would allow an unbounded rendering retry loop.
-- Rendering retries therefore have their own counter, bounded by the same
-- per-job `max_attempts`.
alter table public.jobs
    add column if not exists render_attempts integer not null default 0
    check (render_attempts >= 0);

comment on column public.jobs.render_attempts is
    'Renderings attempted for this job (Phase 5); a parked job is re-claimed while render_attempts < max_attempts.';

-- 2. Lock consistency: rendering is an ACTIVE stage (lease must be held) ------
-- The Phase 4 parked state (analyzing + lock cleared) remains legal.
alter table public.jobs
    drop constraint if exists jobs_lock_consistency;
alter table public.jobs
    add constraint jobs_lock_consistency check (
        (status in ('validating', 'transcribing', 'rendering')
            and locked_by is not null
            and locked_at is not null
            and lease_expires_at is not null)
        or
        (status = 'analyzing'
            and ((locked_by is not null
                  and locked_at is not null
                  and lease_expires_at is not null)
                 or (locked_by is null
                     and locked_at is null
                     and lease_expires_at is null)))
        or
        (status not in ('validating', 'transcribing', 'rendering', 'analyzing')
            and locked_by is null
            and locked_at is null
            and lease_expires_at is null)
    );

comment on constraint jobs_lock_consistency on public.jobs is
    'Lock fields are set exactly while a job is actively worked on (validating, transcribing, rendering, or analyzing with a live lease); a PARKED analyzing job (awaiting rendering) has the lock cleared.';

-- 3. Transition map: + rendering → analyzing ----------------------------------
-- A transient rendering failure (or a graceful release) returns the job to
-- its parked state; the claim RPC then re-enters rendering (bounded by
-- render_attempts). `analyzing → rendering` already exists (Phase 4).
drop trigger if exists jobs_validate_status on public.jobs;
drop function if exists public.validate_job_status_transition();
create function public.validate_job_status_transition()
returns trigger
language plpgsql
as $$
begin
    if old.status = new.status then
        return new;
    end if;

    if (old.status = 'uploading'  and new.status in ('queued', 'failed'))
       or (old.status = 'queued'    and new.status in ('validating', 'failed'))
       or (old.status = 'validating' and new.status in ('ready', 'queued', 'failed'))
       or (old.status = 'ready'      and new.status in ('transcribing', 'failed'))
       or (old.status = 'transcribing' and new.status in ('analyzing', 'ready', 'failed'))
       or (old.status = 'analyzing'  and new.status in ('rendering', 'ready', 'failed'))
       or (old.status = 'rendering'  and new.status in ('completed', 'analyzing', 'failed'))
    then
        return new;
    end if;

    raise exception 'illegal job status transition % -> %', old.status, new.status;
end;
$$;

comment on function public.validate_job_status_transition() is
    'Whitelists legal job status transitions; rendering may return to the parked analyzing state (rendering retry) since Phase 5.';

create trigger jobs_validate_status
    before update of status on public.jobs
    for each row
    execute function public.validate_job_status_transition();

-- 4. Claim: parked analyzing → rendering + expired rendering recovery ---------
-- Claimable (mutually exclusive branches, FIFO by created_at):
--   1. queued / ready            (attempt budget available) — Phases 3-4
--   2. analyzing + lock CLEARED  (PARKED; render budget available) → rendering
--   3. any active stage with an EXPIRED lease (crash recovery, stage kept)
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
     where (j.status in ('queued', 'ready')
            and j.attempt_count < j.max_attempts)
        or (j.status = 'analyzing' and j.locked_by is null
            and j.render_attempts < j.max_attempts)
        or (j.status in ('validating', 'transcribing', 'analyzing', 'rendering')
            and j.lease_expires_at is not null and j.lease_expires_at < now()
            and j.attempt_count < j.max_attempts)
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
           attempt_count = job.attempt_count + 1,
           locked_by = p_worker_id,
           locked_at = now(),
           lease_expires_at = now() + make_interval(secs => p_lease_seconds)
     where id = job.id
     returning * into job;

    return job;
end;
$$;

-- 5. Renewal / progress: valid in rendering too --------------------------------
create or replace function public.renew_job_lease(
    p_job_id uuid,
    p_worker_id text,
    p_lease_seconds integer
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare renewed boolean;
begin
    if p_lease_seconds is null or p_lease_seconds < 30 or p_lease_seconds > 3600 then
        raise exception 'lease seconds must be between 30 and 3600';
    end if;

    update public.jobs
       set lease_expires_at = now() + make_interval(secs => p_lease_seconds)
     where id = p_job_id
       and locked_by = p_worker_id
       and status in ('validating', 'transcribing', 'analyzing', 'rendering')
       and (lease_expires_at is null or lease_expires_at >= now() - interval '5 minutes')
     returning true into renewed;

    return coalesce(renewed, false);
end;
$$;

create or replace function public.update_job_progress(
    p_job_id uuid,
    p_worker_id text,
    p_progress integer
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare updated boolean;
begin
    if p_progress < 0 or p_progress > 100 then
        raise exception 'progress must be between 0 and 100';
    end if;

    update public.jobs
       set progress = p_progress
     where id = p_job_id
       and locked_by = p_worker_id
       and status in ('validating', 'transcribing', 'analyzing', 'rendering')
     returning true into updated;

    return coalesce(updated, false);
end;
$$;

-- 6. Failure: works from rendering too ------------------------------------------
-- Requeue targets: validating → queued; transcribing/analyzing → ready;
-- rendering → analyzing (PARKED: lock cleared) so the claim re-enters
-- rendering; each rendering failure consumes one render_attempt.
create or replace function public.fail_job(
    p_job_id uuid,
    p_worker_id text,
    p_error_category text,
    p_error_message text,
    p_permanent boolean default false
)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
    job public.jobs;
    new_status text;
begin
    select j.* into job from public.jobs j
     where j.id = p_job_id
       and j.locked_by = p_worker_id
       and j.status in ('validating', 'transcribing', 'analyzing', 'rendering')
     for update;

    if not found then
        return 'not_owner';
    end if;

    if length(coalesce(p_error_category, '')) = 0 then
        p_error_category := 'unknown';
    end if;

    if p_permanent
       or job.attempt_count >= job.max_attempts
       or (job.status = 'rendering' and job.render_attempts + 1 >= job.max_attempts)
    then
        new_status := 'failed';
    else
        new_status := case
            when job.status = 'validating' then 'queued'
            when job.status = 'rendering' then 'analyzing'
            else 'ready'
        end;
    end if;

    update public.jobs
       set status = new_status,
           render_attempts = case
               when job.status = 'rendering' then job.render_attempts + 1
               else job.render_attempts
           end,
           last_error = left(p_error_category || ': ' || coalesce(p_error_message, ''), 1000),
           locked_by = null,
           locked_at = null,
           lease_expires_at = null
     where id = job.id;

    return new_status;
end;
$$;

-- 7. Release: rendering releases back to the parked state ------------------------
create or replace function public.release_job(
    p_job_id uuid,
    p_worker_id text
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    released boolean;
    job_status text;
begin
    select status into job_status from public.jobs where id = p_job_id;
    if job_status is null then
        return false;
    end if;

    update public.jobs
       set status = case
               when job_status = 'validating' then 'queued'
               when job_status = 'rendering' then 'analyzing'
               else 'ready'
           end,
           locked_by = null,
           locked_at = null,
           lease_expires_at = null
     where id = p_job_id
       and locked_by = p_worker_id
       and status in ('validating', 'transcribing', 'analyzing', 'rendering')
     returning true into released;

    return coalesce(released, false);
end;
$$;

-- 8. Terminal completion (the ONLY path to `completed` in rendering) -------------
-- Requires ALL of: ownership, status rendering, >= 1 clip, and EVERY clip
-- ready WITH a storage path (rendered AND stored). The DB is the authority —
-- a stale or buggy worker can never mark a job completed early.
create or replace function public.complete_job_rendering(
    p_job_id uuid,
    p_worker_id text
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare done boolean;
begin
    update public.jobs
       set status = 'completed',
           progress = 100,
           completed_at = now(),
           last_error = null,
           locked_by = null,
           locked_at = null,
           lease_expires_at = null
     where id = p_job_id
       and locked_by = p_worker_id
       and status = 'rendering'
       and exists (select 1 from public.clips c where c.job_id = p_job_id)
       and not exists (select 1 from public.clips c
                        where c.job_id = p_job_id
                          and (c.status <> 'ready' or c.storage_path is null))
     returning true into done;

    return coalesce(done, false);
end;
$$;

-- 9. Clip lookup index for the completion guard + resume queries -----------------
create index if not exists clips_job_status_idx
    on public.clips (job_id, status);

-- 10. Privileges: service-role-only for ALL queue-control RPCs --------------------
revoke execute on function
    public.claim_next_job(text, integer),
    public.renew_job_lease(uuid, text, integer),
    public.update_job_progress(uuid, text, integer),
    public.complete_job_ingestion(uuid, text, jsonb),
    public.complete_job_rendering(uuid, text),
    public.fail_job(uuid, text, text, text, boolean),
    public.release_job(uuid, text)
    from public;

grant execute on function
    public.claim_next_job(text, integer),
    public.renew_job_lease(uuid, text, integer),
    public.update_job_progress(uuid, text, integer),
    public.complete_job_ingestion(uuid, text, jsonb),
    public.complete_job_rendering(uuid, text),
    public.fail_job(uuid, text, text, text, boolean),
    public.release_job(uuid, text)
    to service_role;

comment on function public.claim_next_job(text, integer) is
    'Atomically claims the oldest claimable job: queued/ready (attempt budget), PARKED analyzing (render budget → rendering), or an active stage with an expired lease (crash recovery) via FOR UPDATE SKIP LOCKED.';
comment on function public.renew_job_lease(uuid, text, integer) is
    'Extends the lease of a job currently locked by the calling worker (any active stage).';
comment on function public.update_job_progress(uuid, text, integer) is
    'Records 0-100 progress for a job currently locked by the calling worker (any active stage).';
comment on function public.fail_job(uuid, text, text, text, boolean) is
    'Records a failure with a safe category from any active stage; requeues to queued (ingestion), ready (analysis) or the parked analyzing state (rendering, render_attempts + 1) until the budgets are exhausted, then failed.';
comment on function public.release_job(uuid, text) is
    'Gracefully releases a locked job back to its stage entry point (queued, ready, or parked analyzing for rendering).';
comment on function public.complete_job_rendering(uuid, text) is
    'Marks an owned rendering job completed ONLY when at least one clip exists and every clip is ready with a storage path; sets completed_at.';
