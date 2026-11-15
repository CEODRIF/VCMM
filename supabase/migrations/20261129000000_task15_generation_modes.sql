-- =============================================================================
-- TASK 015 P3 — two generation modes + explicit output FPS:
-- jobs.generation_mode, jobs.output_fps, requested_clips generalization,
-- clips.mode, clips duration bounds generalized
-- =============================================================================
-- REPOSITORY-ONLY MIGRATION (manual deployment).
--
-- Per the TASK 015 delivery rules this migration is NOT applied to any
-- production database automatically. It ships with the PR as the reviewed
-- schema delta; an operator applies it manually after explicit approval.
-- The change is strictly ADDITIVE/LOOSENING (defaults preserve the
-- existing behavior for every existing row; no data is modified) and
-- re-applying it is a no-op.
--
-- Semantics
--   jobs.generation_mode:
--     'highlights'    (DEFAULT — every existing row): the intelligent
--                     path. requested_clips is an UPPER BOUND on the
--                     number of highlight clips (1..10 as before).
--     'full_coverage' (Mode A): the ENTIRE source is tiled into
--                     sequential clips of clip_duration_seconds. The clip
--                     COUNT IS DERIVED at analysis time from the probed
--                     source duration (ceil(duration / clip_duration),
--                     final remainder handled by the documented policy) —
--                     requested_clips is stored as NULL and NEVER
--                     truncates the coverage.
--   jobs.output_fps:
--     NULL (default, legacy): source-rate-preserving render (the TASK 007
--     behavior — the output never exceeds the measured source rate).
--     30 | 60: the user's EXPLICIT choice; the render forces that output
--     rate. 60 from a sub-55fps source duplicates frames to reach 60 —
--     the output IS 60fps; it is not (and is never claimed to be) native
--     60fps source material.
--   jobs.requested_clips:
--     becomes NULLABLE: NULL = 'derived at analysis time' (full_coverage).
--     Bound widened to 1..5000 for derived counts (a 24h source at 60s
--     clips = 1440 clips); highlights mode still submits 1..10 from the
--     API (appConfig.clips.maxCount).
--   clips.mode: which generation mode produced the row (display + P4
--     dashboard "Mode: Full Video / Viral Highlight").
--   clips.duration bounds: the generated column (end_time - start_time)
--     generalized from 60..180 to 5..3600 — full-coverage clips may be
--     shorter (the explicit remainder, floored by the worker's 10s merge
--     policy) and at most one full window plus the remainder (<= 2x the
--     180s max user duration, with headroom).
-- =============================================================================

alter table public.jobs
    add column if not exists generation_mode text not null default 'highlights';

alter table public.jobs drop constraint if exists jobs_generation_mode_check;
alter table public.jobs add constraint jobs_generation_mode_check
    check (generation_mode in ('highlights', 'full_coverage'));

alter table public.jobs
    add column if not exists output_fps integer;

alter table public.jobs drop constraint if exists jobs_output_fps_check;
alter table public.jobs add constraint jobs_output_fps_check
    check (output_fps is null or output_fps in (30, 60));

-- requested_clips: NOT NULL 1..10 -> NULLABLE, 1..5000 when present.
alter table public.jobs alter column requested_clips drop not null;
alter table public.jobs drop constraint if exists jobs_requested_clips_check;
alter table public.jobs add constraint jobs_requested_clips_check
    check (requested_clips is null or requested_clips between 1 and 5000);

-- full_coverage jobs derive their count at analysis time: requested_clips
-- must be NULL (it is a derivation slot, not a user request). highlights
-- jobs keep 1..10 (the API enforces the tighter bound).
--
-- NULL-safe form: a bare `between` on a NULL value evaluates to NULL, and
-- a CHECK accepts NULL (only FALSE rejects) — `highlights + NULL` would
-- have slipped through as `NULL OR FALSE`. `is not null` makes the
-- highlights branch evaluate to FALSE for NULL, so the constraint is
-- airtight.
alter table public.jobs drop constraint if exists jobs_mode_count_consistency;
alter table public.jobs add constraint jobs_mode_count_consistency check (
    (generation_mode = 'highlights'
        and requested_clips is not null
        and requested_clips between 1 and 10)
    or (generation_mode = 'full_coverage' and requested_clips is null)
);

alter table public.clips
    add column if not exists mode text not null default 'highlights';

alter table public.clips drop constraint if exists clips_mode_check;
alter table public.clips add constraint clips_mode_check
    check (mode in ('highlights', 'full_coverage'));

alter table public.clips drop constraint if exists clips_duration_bounds;
alter table public.clips add constraint clips_duration_bounds
    check (duration between 5 and 3600);

comment on column public.jobs.generation_mode is
    'TASK 015 P3: ''highlights'' (default; requested_clips 1..10 is an upper bound) or ''full_coverage'' (the whole source tiled into clip_duration_seconds windows; requested_clips NULL = derived at analysis time, never truncates coverage).';
comment on column public.jobs.output_fps is
    'TASK 015 P3: explicit user output frame rate (30 or 60); NULL = legacy source-rate-preserving render (never exceeds the measured source rate). 60 from a low-rate source duplicates frames — honest, not claimed native.';
comment on column public.clips.mode is
    'TASK 015 P3: the generation mode that produced this clip (display: Full Video vs Viral Highlight).';
