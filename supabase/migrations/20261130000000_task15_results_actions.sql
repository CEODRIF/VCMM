-- =============================================================================
-- TASK 015 P4 — results dashboard actions: clip media metadata,
-- permanent-clip-deletion state, job-level cleanup tracking.
--
-- REPOSITORY-ONLY: this file is applied by the test suites (PGlite) and, in
-- a real deployment, by the normal Supabase migration workflow. It is NEVER
-- applied to production by this task.
--
-- Design:
--   * clips.width / height / output_fps / file_size_bytes — the MEASURED
--     output contract (written by the worker from the ffprobe verification
--     that already gates "ready"). NULL until a clip is rendered — the UI
--     shows "—", never a guessed value. File size is also available live
--     from the storage listing; the column is the durable copy.
--   * clips.status gains 'deleting' — the permanent-deletion state machine:
--       pending/processing  -> NEVER deletable (still being produced)
--       ready               -> deleting -> (row removed)
--       failed              -> deleting -> (row removed)
--     The transition is taken ATOMICALLY (UPDATE ... WHERE status in
--     ('ready','failed')), so a concurrent render upload or a double-click
--     delete can never both win. Download/preview only serve 'ready' rows,
--     so a 'deleting' clip is automatically un-downloadable.
--   * jobs.cleanup_error — user-initiated job cleanup partial-failure
--     tracking. Non-NULL means "cleanup started but did not finish"; the
--     row stays un-purged (purged_at NULL), so the cleanup is retryable and
--     the failure stays visible in the results UI. Success clears it and
--     sets purged_at (the Phase 6 idempotency marker, reused).
--
-- Idempotent: safe to re-apply (add column if not exists; conditional
-- constraint rebuild).
-- =============================================================================

-- 1) measured media metadata ----------------------------------------------------

alter table public.clips add column if not exists width int;
alter table public.clips add column if not exists height int;
alter table public.clips add column if not exists output_fps numeric;
alter table public.clips add column if not exists file_size_bytes bigint;

alter table public.clips drop constraint if exists clips_width_positive;
alter table public.clips add constraint clips_width_positive check (width is null or width > 0);

alter table public.clips drop constraint if exists clips_height_positive;
alter table public.clips add constraint clips_height_positive check (height is null or height > 0);

alter table public.clips drop constraint if exists clips_fps_positive;
alter table public.clips add constraint clips_fps_positive check (output_fps is null or output_fps > 0);

alter table public.clips drop constraint if exists clips_file_size_nonnegative;
alter table public.clips add constraint clips_file_size_nonnegative check (file_size_bytes is null or file_size_bytes >= 0);

comment on column public.clips.width is 'Measured output width (ffprobe-verified before ready).';
comment on column public.clips.height is 'Measured output height (ffprobe-verified before ready).';
comment on column public.clips.output_fps is 'Measured output frame rate (avg_frame_rate, ffprobe-verified before ready).';
comment on column public.clips.file_size_bytes is 'Stored object size in bytes (durable copy of the storage listing).';

-- 2) the 'deleting' state --------------------------------------------------------

alter table public.clips drop constraint if exists clips_status_check;
alter table public.clips
    add constraint clips_status_check check (status in ('pending', 'processing', 'ready', 'failed', 'deleting'));

comment on column public.clips.status is
    'pending -> processing -> ready | failed; ready|failed -> deleting (atomic) -> row removed. '
    'deleting blocks download/preview (they only serve ready) and is the concurrency guard for permanent deletion.';

-- 3) job-level cleanup tracking ---------------------------------------------------

alter table public.jobs add column if not exists cleanup_error text;

comment on column public.jobs.cleanup_error is
    'TASK 015 P4: set when a user-initiated job cleanup fails partway (the job then STAYS un-purged and the cleanup is retryable); cleared on success. NULL = no failed cleanup.';
