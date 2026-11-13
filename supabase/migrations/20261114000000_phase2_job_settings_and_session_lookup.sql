-- =============================================================================
-- ClipForge AI — Phase 2: job settings + session lookup index
-- =============================================================================
-- Baseline: 20261113000000_initial_schema.sql (Phase 1) is inspected and
-- preserved. This migration only ADDS:
--   * two job columns needed by the creation API (requested clip duration and
--     caption preference) — enforced by the same 60-180s product rule as the
--     clips table;
--   * an index on processing_sessions.token_hash for the server-side session
--     verification lookup.
-- No existing table, column, or policy is modified or duplicated.
-- =============================================================================

alter table public.jobs
    add column if not exists clip_duration_seconds integer not null default 60;

alter table public.jobs
    add constraint jobs_clip_duration_bounds
    check (clip_duration_seconds between 60 and 180);

alter table public.jobs
    add column if not exists captions_enabled boolean not null default true;

create index if not exists sessions_token_hash_idx
    on public.processing_sessions (token_hash);

comment on column public.jobs.clip_duration_seconds is
    'Requested clip length for this job (60-180 seconds, product rule). Added in Phase 2.';
comment on column public.jobs.captions_enabled is
    'Whether rendered clips should carry animated subtitles. Added in Phase 2.';
comment on index public.sessions_token_hash_idx is
    'Supports server-side session verification (token hash lookup). Added in Phase 2.';
