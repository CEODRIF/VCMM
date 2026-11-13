-- =============================================================================
-- ClipForge AI — Phase 4: AI processing engine (analysis queue + clip scores)
-- =============================================================================
-- Non-destructive: only ADDs columns to `clips`, widens ONE transition
-- (transcribing → ready, so a failed analysis retries from the verified
-- source instead of re-ingesting), and extends the queue RPCs to cover the
-- Phase 4 stages (transcribing / analyzing). Existing rows and behavior are
-- preserved.
--
-- Applies AFTER 20261115000001 on the same verified development project.
-- Executed + asserted against a real PostgreSQL engine in the test suite
-- (tests/database/phase4-queue.test.ts). Never applied to production.
-- =============================================================================

-- 1. Clip analysis columns ----------------------------------------------------
-- `score`  : explainable ranking score, 0-100 (default 0 for pre-Phase-4 rows).
-- `scoring`: the per-criterion breakdown + human-readable reasons (jsonb).
alter table public.clips
    add column if not exists score double precision not null default 0;

alter table public.clips
    drop constraint if exists clips_score_bounds;
alter table public.clips
    add constraint clips_score_bounds check (score between 0 and 100);

alter table public.clips
    add column if not exists scoring jsonb;

comment on column public.clips.score is
    'Explainable ranking score for the detected clip (0-100). 0 for rows created before Phase 4.';
comment on column public.clips.scoring is
    'Per-criterion score breakdown (hook, completeness, coherence, engagement, duration fit) with reasons.';

-- 2. Lock consistency: Phase 4 jobs also hold their lease while
-- transcribing/analyzing (a parked analyzing job has the lock cleared, so it
-- is never confused with an active one).
alter table public.jobs
    drop constraint if exists jobs_lock_consistency;
alter table public.jobs
    add constraint jobs_lock_consistency check (
        (status in ('validating', 'transcribing')
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
        (status not in ('validating', 'transcribing', 'analyzing')
            and locked_by is null
            and locked_at is null
            and lease_expires_at is null)
    );

comment on constraint jobs_lock_consistency on public.jobs is
    'Lock fields are set exactly while a job is actively worked on (validating, transcribing, or analyzing with a live lease); a PARKED analyzing job (Phase 4 finished, rendering pending) has the lock cleared.';

-- 3. Transition map: allow transcribing → ready --------------------------------
-- A TRANSIENT analysis failure (download/ffmpeg/transcription error) returns
-- the job to `ready` (the source is still verified in storage) instead of
-- re-running ingestion from `queued`. The DB trigger remains the authority on
-- which transitions are legal — no client or worker can skip stages.
-- (Numbering note: sections below are 4 = RPCs, 5 = privileges.)
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
       or (old.status = 'rendering'  and new.status in ('completed', 'failed'))
    then
        return new;
    end if;

    raise exception 'illegal job status transition % -> %', old.status, new.status;
end;
$$;

comment on function public.validate_job_status_transition() is
    'Whitelists legal job status transitions; transcribing may return to ready (analysis retry) since Phase 4.';

-- Recreate the trigger on the (new) function.
create trigger jobs_validate_status
    before update of status on public.jobs
    for each row
    execute function public.validate_job_status_transition();

-- 4. Queue RPCs: extended to the Phase 4 stages --------------------------------
-- Same signatures as Phase 3 (backward compatible); only the allowed status
-- sets change, plus stage-aware claim/requeue targets.

-- Claim: claimable = queued, ready, OR (validating|transcribing|analyzing)
-- with an EXPIRED lease (crash recovery for every active stage). A `ready`
-- job enters the analysis path as `transcribing`; a `queued` job starts
-- ingestion as `validating`; a recovered job keeps its stage.
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
            or (j.status in ('validating', 'transcribing', 'analyzing')
                and j.lease_expires_at is not null and j.lease_expires_at < now()))
       and j.attempt_count < j.max_attempts
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

-- Lease renewal: valid for every active stage.
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
       and status in ('validating', 'transcribing', 'analyzing')
       and (lease_expires_at is null or lease_expires_at >= now() - interval '5 minutes')
     returning true into renewed;

    return coalesce(renewed, false);
end;
$$;

-- Progress reporting: valid for every active stage.
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
       and status in ('validating', 'transcribing', 'analyzing')
     returning true into updated;

    return coalesce(updated, false);
end;
$$;

-- Failure handling: works from every active stage. A transient failure
-- requeues to `queued` for ingestion-stage jobs and to `ready` for
-- analysis-stage jobs (the source is still verified — no re-ingestion).
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
       and j.status in ('validating', 'transcribing', 'analyzing')
     for update;

    if not found then
        return 'not_owner';
    end if;

    if length(coalesce(p_error_category, '')) = 0 then
        p_error_category := 'unknown';
    end if;

    if p_permanent or job.attempt_count >= job.max_attempts then
        new_status := 'failed';
    else
        new_status := case
            when job.status = 'validating' then 'queued'
            else 'ready'  -- transcribing/analyzing: retry analysis, not ingestion
        end;
    end if;

    update public.jobs
       set status = new_status,
           last_error = left(p_error_category || ': ' || coalesce(p_error_message, ''), 1000),
           locked_by = null,
           locked_at = null,
           lease_expires_at = null
     where id = job.id;

    return new_status;
end;
$$;

-- Graceful release: back to the stage's entry point (queued for ingestion,
-- ready for analysis).
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
               else 'ready'
           end,
           locked_by = null,
           locked_at = null,
           lease_expires_at = null
     where id = p_job_id
       and locked_by = p_worker_id
       and status in ('validating', 'transcribing', 'analyzing')
     returning true into released;

    return coalesce(released, false);
end;
$$;

-- Privileges: keep the Phase 3 restriction (defense in depth after replace).
revoke execute on function
    public.claim_next_job(text, integer),
    public.renew_job_lease(uuid, text, integer),
    public.update_job_progress(uuid, text, integer),
    public.complete_job_ingestion(uuid, text, jsonb),
    public.fail_job(uuid, text, text, text, boolean),
    public.release_job(uuid, text)
    from public;

grant execute on function
    public.claim_next_job(text, integer),
    public.renew_job_lease(uuid, text, integer),
    public.update_job_progress(uuid, text, integer),
    public.complete_job_ingestion(uuid, text, jsonb),
    public.fail_job(uuid, text, text, text, boolean),
    public.release_job(uuid, text)
    to service_role;

comment on function public.claim_next_job(text, integer) is
    'Atomically claims the oldest claimable job (queued, ready, or an active stage with an expired lease = crash recovery) via FOR UPDATE SKIP LOCKED; stage-aware entry transition.';
comment on function public.renew_job_lease(uuid, text, integer) is
    'Extends the lease of a job currently locked by the calling worker (any active stage).';
comment on function public.fail_job(uuid, text, text, text, boolean) is
    'Records a failure with a safe category from any active stage; requeues to queued (ingestion) or ready (analysis) until max_attempts, then failed.';
comment on function public.release_job(uuid, text) is
    'Gracefully releases a locked job back to its stage entry point (queued or ready).';
