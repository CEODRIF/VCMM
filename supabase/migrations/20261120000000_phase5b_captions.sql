-- =============================================================================
-- ClipForge AI — Phase 5B: word-level caption payloads on clips
-- =============================================================================
-- Non-destructive and additive:
--   * adds ONE nullable column to `clips` (`captions jsonb`) carrying the
--     REAL word-level transcription timings for the clip window, so the
--     rendering stage (Phase 5B) can burn animated subtitles WITHOUT
--     re-transcribing or inventing anything,
--   * no existing rows, columns, RPCs, grants or RLS policies are modified.
--
-- Shape (written by the analysis stage, read by the rendering stage):
--     {"words": [{"w": "<word>", "s": <abs_start_sec>, "e": <abs_end_sec>}, ...]}
-- with ABSOLUTE (audio-time) seconds; `s < e`; words are clipped to the
-- [start_time, end_time] window. NULL = the window has no usable words
-- (the clip then renders without subtitles — the Phase 5A behavior).
--
-- The job-level `captions_enabled` toggle already exists (Phase 2 column on
-- `jobs`) and needs no change.
--
-- Applies AFTER 20261117000000 on the same verified development project.
-- Executed + asserted against a real PostgreSQL engine in the test suite
-- (tests/database/phase5b-captions.test.ts). Never applied to production.
-- =============================================================================

alter table public.clips
    add column if not exists captions jsonb;

comment on column public.clips.captions is
    'Word-level caption payload for the clip (Phase 5B): {"words":[{"w","s","e"}...]} with ABSOLUTE audio timestamps clipped to the clip window; NULL = no subtitles for this clip. Written by the analysis stage, consumed by the renderer.';
