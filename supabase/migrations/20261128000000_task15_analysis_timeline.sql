-- =============================================================================
-- TASK 015 — full-video intelligence engine:
-- analysis_timeline (the unified, coverage-checked source timeline)
-- =============================================================================
-- REPOSITORY-ONLY MIGRATION (manual deployment).
--
-- Per the TASK 015 delivery rules this migration is NOT applied to any
-- production database automatically. It ships with the PR as the reviewed
-- schema delta; an operator applies it manually after explicit approval
-- (e.g. via the Supabase migration runner on the staging/production
-- project). The change is strictly ADDITIVE (one new table + index; no
-- existing column, row, or constraint changes) and re-applying it is a
-- no-op (create ... if not exists).
--
-- What it stores
--   One row per sequential window over the WHOLE source duration
--   (window_index 0..N-1; the windows tile [0, duration] with no gaps and
--   no overlaps — enforced in the worker before the upsert, and re-checked
--   on resume). The table is the persisted proof of full-timeline coverage:
--   every source second belongs to exactly one row.
--
-- Honesty contract (mirrors the worker's engine):
--   * transcript        — ACTUAL transcribed text overlapping the window
--                         (empty string when nothing was transcribed there;
--                         never invented filler);
--   * scene_description / event_type / event_importance / candidate_clip —
--                         set ONLY from a completed Gemini assessment
--                         (gemini_assessment.status = 'ok'). When Gemini is
--                         unconfigured or the window assessment failed,
--                         gemini_assessment records the honest state
--                         ('not_configured' / 'failed' + reason) and the
--                         semantic fields stay null/false — never guessed;
--   * visual_evidence / audio_evidence — MEASURED local signals (the Phase 7
--                         candidate events overlapping the window);
--   * confidence        — peak MEASURED signal strength in the window, or
--                         null when no signal was measured.
--
-- Storage policy: METADATA ONLY. No media bytes, no source video content —
-- the media itself lives in object storage (sources/ and clips/ buckets).
-- RLS: deny-by-default (no policies); only the service-role worker client
-- writes/reads it (same contract as candidate_events / processing_checkpoints).
-- =============================================================================

create table if not exists public.analysis_timeline (
    job_id            uuid        not null references public.jobs (id) on delete cascade,
    window_index      integer     not null check (window_index >= 0),
    start_time        double precision not null check (start_time >= 0),
    end_time          double precision not null check (end_time >= start_time),
    event_type        text        not null default 'unknown',
    transcript        text        not null default '',
    scene_description text,
    visual_evidence   jsonb       not null default '{}'::jsonb,
    audio_evidence    jsonb       not null default '{}'::jsonb,
    event_importance  numeric     check (event_importance is null or (event_importance >= 0 and event_importance <= 1)),
    candidate_clip    boolean     not null default false,
    confidence        numeric     check (confidence is null or (confidence >= 0 and confidence <= 1)),
    gemini_assessment jsonb,
    created_at        timestamptz not null default now(),
    primary key (job_id, window_index)
);

create index if not exists analysis_timeline_job_start_idx
    on public.analysis_timeline (job_id, start_time);

comment on table public.analysis_timeline is
    'TASK 015: the unified full-timeline analysis — one row per sequential window over [0, source duration] (no gaps, no overlaps; the persisted coverage proof). Semantic fields are set only from completed Gemini assessments (honest state otherwise); evidence fields hold measured local signals; metadata only, no media bytes.';

alter table public.analysis_timeline enable row level security;
