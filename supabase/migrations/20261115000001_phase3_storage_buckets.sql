-- =============================================================================
-- ClipForge AI — Phase 3: private Supabase Storage buckets
-- =============================================================================
-- Supabase-specific (references the storage schema). Apply AFTER
-- 20261115000000_phase3_queue_and_ingestion.sql on the same development
-- project. The file is executed against a real Postgres engine in the test
-- suite (tests/database/storage-buckets.test.ts, on a schema stub) and
-- applied only to a verified development Supabase project — never production.
--
-- Fixed UUIDs are used as bucket ids (storage.buckets.id is a uuid column)
-- so the inserts stay deterministic and idempotent across re-runs.
--
-- Security model:
--   * Both buckets are PRIVATE (public = false).
--   * NO storage policies are created for anon/authenticated roles, so all
--     object access by clients is denied by default (RLS on storage.objects).
--   * Only the service_role (BYPASSRLS) — held exclusively by the Next.js
--     server and the Python worker — can read/write these buckets.
--   * Source videos are NEVER publicly accessible; the clips bucket is
--     served only via short-lived signed URLs generated server-side.
-- =============================================================================

insert into storage.buckets (id, name, public)
values (
    'b2f3a4c5-0000-4a6e-9b1d-100000000001',  -- clipforge-sources
    'clipforge-sources',
    false
)
on conflict (id) do update set name = excluded.name, public = false;

insert into storage.buckets (id, name, public)
values (
    'b2f3a4c5-0000-4a6e-9b1d-100000000002',  -- clipforge-clips
    'clipforge-clips',
    false
)
on conflict (id) do update set name = excluded.name, public = false;

-- Defense in depth: if a public read policy was ever added to these buckets,
-- drop it. (No-ops when none exists.)
drop policy if exists "public_read_sources" on storage.objects;
drop policy if exists "public_read_clips" on storage.objects;

comment on table storage.buckets is
    'Includes ClipForge private buckets: clipforge-sources (original uploads) and clipforge-clips (rendered output). Both private; service-role-only access.';
