-- =============================================================================
-- ClipForge AI — Phase 7 (TASK 007): stream intelligence foundation
-- =============================================================================
-- Non-destructive and additive:
--   * adds TWO worker-owned tables (`processing_checkpoints`,
--     `candidate_events`) so long-recording analysis can be chunked,
--     checkpointed and resumed WITHOUT reprocessing completed work and
--     WITHOUT duplicating candidate events on retry,
--   * extends the existing `jobs.source_type` check with 'twitch'
--     (additive — the two existing values remain valid) so a future
--     Twitch adapter needs no further migration,
--   * no existing rows, columns (beyond the check), RPCs, grants or RLS
--     policies are modified; both new tables get deny-by-default RLS with
--     NO policies (only the service-role worker client touches them, the
--     same pattern as jobs/clips).
--
-- processing_checkpoints — one row per COMPLETED analysis chunk:
--   * primary key (job_id, chunk_index): the chunk plan is deterministic
--     f(source_duration, chunk_seconds, overlap_seconds), so "resume" =
--     plan minus these rows; a finished chunk is never reprocessed;
--   * `features jsonb` stores the COMPACT per-chunk signal summary
--     (1 Hz RMS/ZCR downsample + silence gaps + the chunk's own signal
--     events) — NOT raw audio/video data. This keeps fusion input
--     O(chunks) and small for arbitrarily long recordings;
--   * RLS enabled, no policies (service role only).
--
-- candidate_events — multimodal candidate events detected during chunking:
--   * `event_key` (stable hash of job | start-time bucket | signal family
--     | kind) makes retries EXACT-DEDUPLICATE: upserting the same event
--     again changes nothing, a new event gets a new key;
--   * `evidence jsonb` carries the MEASURED signals behind the event
--     (family strengths, window stats) — nothing is fabricated, and the
--     ranking breakdown stays explainable;
--   * `score`/`confidence` are filled by the fusion/selection pass;
--     raw detector events start at the fusion-assigned values;
--   * RLS enabled, no policies (service role only).
--
-- jobs.source_type: the check constraint is replaced to ADD 'twitch'.
-- This is additive for data (existing rows keep validating) and enables
-- the provider architecture; the Twitch adapter itself does not ship in
-- this migration (see docs/stream-intelligence.md §8).
--
-- Application: run in a VERIFIED development Supabase project after the
-- Phase 6 migration (supabase dashboard → SQL editor, or the CLI). Never
-- applied to production. Executed + asserted against a real PostgreSQL
-- engine in the test suite (tests/database/phase7-intel.test.ts).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1) processing_checkpoints
-- -----------------------------------------------------------------------------
create table if not exists public.processing_checkpoints (
    job_id      uuid        not null references public.jobs (id) on delete cascade,
    chunk_index integer     not null check (chunk_index >= 0),
    start_time  double precision not null check (start_time >= 0),
    end_time    double precision not null check (end_time > start_time),
    -- A row existing IS the "done" state (the table holds no other status);
    -- the default + check make partial writes impossible by construction.
    status      text        not null default 'done' check (status = 'done'),
    features    jsonb       not null default '{}'::jsonb,
    updated_at  timestamptz not null default now(),
    primary key (job_id, chunk_index)
);

create index if not exists processing_checkpoints_job_idx
    on public.processing_checkpoints (job_id);

comment on table public.processing_checkpoints is
    'Phase 7: per-chunk analysis checkpoints for long-recording stream intelligence. A chunk is present iff its features+events are durably stored; resume = deterministic plan minus these rows (no reprocessing, no partial chunks).';
comment on column public.processing_checkpoints.features is
    'Compact per-chunk signal summary (1 Hz RMS/ZCR downsample, silence gaps, detected signal events with absolute timestamps). Never raw media.';

alter table public.processing_checkpoints enable row level security;

-- -----------------------------------------------------------------------------
-- 2) candidate_events
-- -----------------------------------------------------------------------------
create table if not exists public.candidate_events (
    job_id      uuid        not null references public.jobs (id) on delete cascade,
    event_key   text        not null,
    start_time  double precision not null check (start_time >= 0),
    end_time    double precision not null check (end_time >= start_time),
    category    text        not null default 'unclassified',
    score       numeric     not null default 0 check (score >= 0 and score <= 100),
    confidence  numeric     not null default 0 check (confidence >= 0 and confidence <= 1),
    evidence    jsonb       not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    primary key (job_id, event_key)
);

create index if not exists candidate_events_job_start_idx
    on public.candidate_events (job_id, start_time);

comment on table public.candidate_events is
    'Phase 7: multimodal candidate events (audio energy/transients/voice-activity + visual scene/motion) detected per chunk. event_key is a stable hash so retries exact-dedupe; evidence holds the measured signals behind each event (no fabricated data).';

alter table public.candidate_events enable row level security;

-- -----------------------------------------------------------------------------
-- 3) jobs.source_type: ADD 'twitch' (additive check replacements)
-- -----------------------------------------------------------------------------
-- The value check gains 'twitch'; the source-presence check gains the
-- matching branch (a twitch job carries its VOD URL in source_url, like
-- youtube). Both replacements are additive for existing rows.
alter table public.jobs drop constraint if exists jobs_source_type_check;
alter table public.jobs add constraint jobs_source_type_check
    check (source_type in ('youtube', 'upload', 'twitch'));

alter table public.jobs drop constraint if exists jobs_source_present;
alter table public.jobs add constraint jobs_source_present check (
    (source_type = 'youtube' and source_url is not null)
    or (source_type = 'upload' and source_path is not null)
    or (source_type = 'twitch' and source_url is not null)
);
