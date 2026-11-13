-- =============================================================================
-- ClipForge AI — Phase 3: processing queue + ingestion lifecycle
-- =============================================================================
-- Baseline: 20261113000000 (Phase 1) and 20261114000000 (Phase 2) are
-- inspected and preserved. This migration only ADDS queue/lease columns,
-- widens the status state machine, adds transition + updated_at triggers,
-- and restricted RPCs for worker job claiming.
--
-- State machine (ingestion scope of Phase 3):
--   upload jobs:  uploading → queued → validating → ready
--   all jobs:     (any active) → failed
-- The product pipeline continues ready → transcribing → analyzing →
-- rendering → completed (Phases 4-5); those states are already part of the
-- constraint and transition map.
-- =============================================================================

-- 1. Queue / lease / ingestion columns ---------------------------------------
alter table public.jobs
    add column if not exists attempt_count integer not null default 0
    check (attempt_count >= 0);

alter table public.jobs
    add column if not exists max_attempts integer not null default 3
    check (max_attempts >= 1);

alter table public.jobs
    add column if not exists locked_at timestamptz;

alter table public.jobs
    add column if not exists locked_by text;

alter table public.jobs
    add column if not exists lease_expires_at timestamptz;

alter table public.jobs
    add column if not exists last_error text;

alter table public.jobs
    add column if not exists upload_completed_at timestamptz;

alter table public.jobs
    add column if not exists upload_auth_expires_at timestamptz;

alter table public.jobs
    add column if not exists source_metadata jsonb not null default '{}'::jsonb;

alter table public.jobs
    add column if not exists updated_at timestamptz not null default now();

-- 2. Widen the status constraint: 7 states → 9 (adds validating, ready) ------
alter table public.jobs drop constraint if exists jobs_status_check;
alter table public.jobs
    add constraint jobs_status_check check (status in (
        'uploading', 'queued', 'validating', 'transcribing', 'analyzing',
        'rendering', 'ready', 'completed', 'failed'
    ));

-- Lock fields must be set exactly while a job is being worked on.
alter table public.jobs
    add constraint jobs_lock_consistency check (
        (status = 'validating'
            and locked_by is not null
            and locked_at is not null
            and lease_expires_at is not null)
        or
        (status <> 'validating'
            and locked_by is null
            and locked_at is null
            and lease_expires_at is null)
    );

-- Partial index for the claim hot path.
create index if not exists jobs_claim_idx
    on public.jobs (created_at)
    where status = 'queued';

-- 3. updated_at maintenance ----------------------------------------------------
create or replace function public.set_jobs_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists jobs_set_updated_at on public.jobs;
create trigger jobs_set_updated_at
    before update on public.jobs
    for each row
    execute function public.set_jobs_updated_at();

-- 4. Status transition enforcement --------------------------------------------
-- Any status change (by any role, including the API service role) must be a
-- legal transition. This makes arbitrary client-driven status changes
-- impossible at the database level.
create or replace function public.validate_job_status_transition()
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
       or (old.status = 'transcribing' and new.status in ('analyzing', 'failed'))
       or (old.status = 'analyzing'  and new.status in ('rendering', 'failed'))
       or (old.status = 'rendering'  and new.status in ('completed', 'failed'))
    then
        return new;
    end if;

    raise exception 'illegal job status transition % -> %', old.status, new.status;
end;
$$;

drop trigger if exists jobs_validate_status on public.jobs;
create trigger jobs_validate_status
    before update of status on public.jobs
    for each row
    execute function public.validate_job_status_transition();

-- 5. Worker queue RPCs ----------------------------------------------------------
-- SECURITY DEFINER: owned by the database owner, so the worker (acting with
-- service_role) can read/write queue fields without granting broad table
-- access. EXECUTE is revoked from PUBLIC and granted only to service_role.

-- Claim the oldest claimable job atomically (FOR UPDATE SKIP LOCKED).
-- A job is claimable when:
--   * it is 'queued', OR it is 'validating' with an EXPIRED lease
--     (a crashed/stalled worker abandoned it — crash recovery), and
--   * its attempt budget is not exhausted.
-- An active (unexpired) validating job is never taken from its worker.
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
     where j.status in ('queued', 'validating')
       and (j.lease_expires_at is null or j.lease_expires_at < now())
       and j.attempt_count < j.max_attempts
     order by j.created_at
     limit 1
     for update skip locked;

    if not found then
        return null;
    end if;

    update public.jobs
       set status = 'validating',
           attempt_count = attempt_count + 1,
           locked_by = p_worker_id,
           locked_at = now(),
           lease_expires_at = now() + make_interval(secs => p_lease_seconds)
     where id = job.id
     returning * into job;

    return job;
end;
$$;

-- Renew the lease of a job the caller currently owns.
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
       and status = 'validating'
       and (lease_expires_at is null or lease_expires_at >= now() - interval '5 minutes')
    returning true into renewed;

    return coalesce(renewed, false);
end;
$$;

-- Report progress (0-100) for a job the caller currently owns.
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
       and status = 'validating'
    returning true into updated;

    return coalesce(updated, false);
end;
$$;

-- Mark ingestion complete: validating → ready. Clears the lock.
create or replace function public.complete_job_ingestion(
    p_job_id uuid,
    p_worker_id text,
    p_source_metadata jsonb default null
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare done boolean;
begin
    update public.jobs
       set status = 'ready',
           progress = 100,
           source_metadata = case
               when p_source_metadata is null then source_metadata
               else source_metadata || p_source_metadata
           end,
           locked_by = null,
           locked_at = null,
           lease_expires_at = null,
           last_error = null
     where id = p_job_id
       and locked_by = p_worker_id
       and status = 'validating'
    returning true into done;

    return coalesce(done, false);
end;
$$;

-- Fail a job: bounded retries, then terminal failed. Keeps a safe error category.
-- p_permanent=true marks unrecoverable errors (e.g. corrupt media) that must
-- not be retried, regardless of remaining attempts.
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
       and j.status = 'validating'
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
        new_status := 'queued';
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

-- Graceful release: validating → queued, keep the lock cleared.
create or replace function public.release_job(
    p_job_id uuid,
    p_worker_id text
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare released boolean;
begin
    update public.jobs
       set status = 'queued',
           locked_by = null,
           locked_at = null,
           lease_expires_at = null
     where id = p_job_id
       and locked_by = p_worker_id
       and status = 'validating'
    returning true into released;

    return coalesce(released, false);
end;
$$;

-- Restrict queue-control RPCs: only the trusted service role.
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
    'Atomically claims the oldest claimable job (queued, or validating with an expired lease = crash recovery) for a worker (FOR UPDATE SKIP LOCKED). Worker/lease management only.';
comment on function public.renew_job_lease(uuid, text, integer) is
    'Extends the lease of a job currently locked by the calling worker.';
comment on function public.update_job_progress(uuid, text, integer) is
    'Records 0-100 progress for a job currently locked by the calling worker.';
comment on function public.complete_job_ingestion(uuid, text, jsonb) is
    'Marks a locked job ready (ingestion complete) and merges verified source metadata.';
comment on function public.fail_job(uuid, text, text, text, boolean) is
    'Records a failure with a safe category; requeues until max_attempts (or immediately for permanent errors), then failed.';
comment on function public.release_job(uuid, text) is
    'Gracefully releases a locked job back to the queue.';
