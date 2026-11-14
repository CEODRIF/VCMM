-- =============================================================================
-- TASK 014 — universal video link ingestion:
-- jobs.source_type += 'direct_url'
-- =============================================================================
-- REPOSITORY-ONLY MIGRATION (manual deployment).
--
-- Per the TASK 014 delivery rules this migration is NOT applied to any
-- production database automatically. It ships with the PR as the reviewed
-- schema delta; an operator applies it manually after explicit approval
-- (e.g. via the Supabase migration runner on the staging/production
-- project). The change is strictly ADDITIVE (two CHECK constraints gain a
-- branch; no column, row, or index changes) and re-applying it is a no-op.
--
-- Delta:
--   1) jobs_source_type_check — accepts the new 'direct_url' source type
--      (public http(s) links to a video file — .mp4/.webm/.mov — imported
--      by the worker through the SSRF-guarded streaming path).
--   2) jobs_source_present — the matching branch: a direct_url job carries
--      its link in source_url (exactly like youtube/twitch jobs).
--
-- Existing rows and the youtube/upload/twitch behavior are untouched.
-- =============================================================================

alter table public.jobs drop constraint if exists jobs_source_type_check;
alter table public.jobs add constraint jobs_source_type_check
    check (source_type in ('youtube', 'upload', 'twitch', 'direct_url'));

alter table public.jobs drop constraint if exists jobs_source_present;
alter table public.jobs add constraint jobs_source_present check (
    (source_type = 'youtube' and source_url is not null)
    or (source_type = 'upload' and source_path is not null)
    or (source_type = 'twitch' and source_url is not null)
    or (source_type = 'direct_url' and source_url is not null)
);
