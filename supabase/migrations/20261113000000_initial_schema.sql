-- =============================================================================
-- ClipForge AI — Phase 1 initial schema
-- =============================================================================
-- STATUS: version-controlled and reviewed, NOT applied to any database yet.
-- Apply only to a development Supabase project when Phase 2 begins.
--
-- Design principles:
--   * UUID primary keys (gen_random_uuid, built into Postgres 13+).
--   * Timestamps in timestamptz.
--   * Every table has Row Level Security enabled and NO anon policies, so
--     direct client access is denied by default. The backend API (using the
--     service role, server-side only) is the single mediator of access.
-- =============================================================================

-- processing_sessions ---------------------------------------------------------
-- Short-lived access sessions created when the shared access code is verified
-- server-side (Phase 2). Only the hash of the session token is stored.
create table if not exists public.processing_sessions (
    id uuid primary key default gen_random_uuid(),
    token_hash text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    constraint processing_sessions_expiry_future check (expires_at > created_at)
);

-- jobs ------------------------------------------------------------------------
-- One row per clip-creation request. `status` drives the processing state
-- machine; `progress` is 0-100.
create table if not exists public.jobs (
    id uuid primary key default gen_random_uuid(),
    session_id uuid not null references public.processing_sessions (id) on delete cascade,
    source_type text not null check (source_type in ('youtube', 'upload')),
    source_url text,
    source_path text,
    requested_clips integer not null check (requested_clips between 1 and 10),
    status text not null default 'queued' check (status in (
        'queued', 'uploading', 'transcribing', 'analyzing', 'rendering', 'completed', 'failed'
    )),
    progress integer not null default 0 check (progress between 0 and 100),
    error_message text,
    created_at timestamptz not null default now(),
    completed_at timestamptz,
    expires_at timestamptz,
    constraint jobs_source_present check (
        (source_type = 'youtube' and source_url is not null)
        or (source_type = 'upload' and source_path is not null)
    ),
    constraint jobs_completed_consistency check (
        (status = 'completed' and completed_at is not null)
        or (status <> 'completed' and completed_at is null)
    )
);

-- clips -----------------------------------------------------------------------
-- Detected and rendered clips belonging to a job. `duration` is derived from
-- the timestamps and bounded by the product rule (60-180 seconds).
create table if not exists public.clips (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references public.jobs (id) on delete cascade,
    title text not null,
    start_time numeric not null check (start_time >= 0),
    end_time numeric not null,
    duration numeric not null generated always as (end_time - start_time) stored,
    storage_path text,
    status text not null default 'pending' check (status in ('pending', 'processing', 'ready', 'failed')),
    sort_order integer not null default 0,
    created_at timestamptz not null default now(),
    constraint clips_end_after_start check (end_time > start_time),
    constraint clips_duration_bounds check (duration between 60 and 180)
);

-- indexes ---------------------------------------------------------------------
create index if not exists sessions_expires_at_idx on public.processing_sessions (expires_at);
create index if not exists jobs_session_id_idx on public.jobs (session_id);
create index if not exists jobs_status_idx on public.jobs (status);
create index if not exists jobs_expires_at_idx on public.jobs (expires_at);
create index if not exists clips_job_sort_idx on public.clips (job_id, sort_order);

-- security --------------------------------------------------------------------
-- Deny-by-default: RLS on, no policies for the anon role. The API server
-- (service role) mediates all reads and writes.
alter table public.processing_sessions enable row level security;
alter table public.jobs enable row level security;
alter table public.clips enable row level security;

-- documentation -----------------------------------------------------------------
comment on table public.processing_sessions is
    'Access sessions for the shared access code. token_hash is the SHA-256 hash of the session token; plaintext is never stored.';
comment on table public.jobs is
    'One row per clip-creation request. Drives the processing state machine (queued -> uploading -> transcribing -> analyzing -> rendering -> completed|failed).';
comment on table public.clips is
    'Detected and rendered clips belonging to a job. duration is end_time - start_time, bounded to 60-180 seconds by product rule.';
