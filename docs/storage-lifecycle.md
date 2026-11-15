# Storage Lifecycle — Buckets, States, Deletion, Cleanup

## Buckets & object layout

Two **private** Supabase Storage buckets (no client policies —
deny-by-default; only `service_role`, BYPASSRLS, reads/writes):

| Bucket | Objects |
|---|---|
| `clipforge-sources` | `sources/{job_id}/original/{uuid}.mp4` — the ingested source (upload, direct URL, or authorized Twitch clip download) |
| `clipforge-clips` | `clips/{job_id}/{clip_id}.mp4` — rendered clips |

The **canonical path conventions are load-bearing**: every read,
download and deletion cross-checks the stored path against the exact
shape derived from the verified `(job_id, clip_id)` pair
(`validateClipPath` / `validateSourcePath`). A corrupt or foreign
`storage_path` is never signed, never streamed, never deleted.

## State model

- **Source**: exists from materialization until (a) the configured
  retention policy deletes it after successful completion, or (b)
  job-level cleanup, or (c) the Phase 6 purge. It is **never removed
  before every clip is `ready` + stored** — the retention hook runs only
  after `complete_job_rendering` accepted.
- **Clips**: `pending → processing → ready` (or `failed`). P4 adds
  `deleting` as a terminal-in-progress state owned by exactly one
  request (atomic conditional update — see below).
- **Temp render files**: worker-local, deleted on clip
  completion/failure and by the stale-temp sweep on startup
  (`WORKER_WORKER_TMP_STALE_SECONDS`).
- **Failed uploads**: the job row stays failed; a source object without
  a `ready`-dependent claim is eligible for orphan cleanup only after
  its grace period AND only when the owning job row is gone.
- **Pending deletions**: the `deleting` clip state; if the process dies
  between the status transition and the row deletion, the object is
  already gone (deletion order below) and the row is re-deletable
  idempotently.

## Source retention policy (P5-configurable, no secret changes)

`WORKER_SOURCE_RETENTION` = `keep` (default) | `delete_on_complete`.

- `keep` — the source stays until cleanup/purge (re-processing of the
  same job could reuse it).
- `delete_on_complete` — after **all** clips are ready + stored and the
  DB accepted completion, the worker deletes the source object
  (verified absence) and logs it. Failure to delete the source **does
  not fail the finished job** (artifacts are complete; Phase 6 purge is
  the backstop). Generated clips live in a separate bucket and are
  fully independent of source deletion.

## Permanent clip deletion (P4 API)

`DELETE /api/v1/jobs/{jobId}/clips/{clipId}` — protocol:

1. Session + ownership (`requireOwnedJobForClip`; uniform 404 for
   foreign/unknown/undeletable).
2. 409 while the clip is `pending`/`processing` — **except** a stuck
   clip of a **terminal** job whose lease expired (crashed worker).
3. **Atomic** `UPDATE clips SET status='deleting' WHERE status=<exact
   verified status>` — from that instant preview/download stop serving
   the clip (they only serve `ready`), and a concurrent second delete
   gets 409.
4. Storage: `HEAD → DELETE → HEAD`. Already missing = idempotent retry.
   Deletion **verified absent** = success; anything else (transport
   error, object still present) → status **restored** to its previous
   value and the API answers **502 — deletion success is never reported
   without verified absence**.
5. Row deleted guarded on `status='deleting'`.

## Job-level cleanup (P4 API)

`POST /api/v1/jobs/{jobId}/cleanup` with body `{ "confirm": true }`:

- terminal jobs only (409 while active); ownership verified;
- safe order: **clips first** (each via the verified-deletion protocol;
  rows already `deleting` are skipped — the concurrent single-delete
  request owns them) → **source object** → **purge marker**
  (`jobs.purged_at`, Phase 6 semantics reused; storage-before-DB is
  crash-safe at-least-once);
- **partial failure**: `jobs.cleanup_error` records what failed, the job
  stays **un-purged**, the UI shows a visible "Cleanup incomplete —
  retry available" banner with Retry, and the response is retryable —
  already-deleted objects are simply `missing` on retry (idempotent);
- a fully purged job re-answered `alreadyPurged`.

## Orphan & expiry cleanup (manual, bounded — no scheduler)

`python -m app.cleanup` (worker) — the existing Phase 6 purge CLI
(expired terminal jobs: clips + source objects, then `purged_at`).

P4 adds orphan mode: `python -m app.cleanup --orphans
[--orphan-grace-minutes N]` (default 60).

Safety contract (all unit-tested):

- scans **only** the two private buckets, only the canonical
  `clips/{job}/…` and `sources/{job}/original/…` shapes — unrelated
  buckets/objects are never touched;
- per object: canonical shape gate → **owner-row check** (clip
  `(job_id, id)` / job row) with **assume-PRESENT-on-error** (a failing
  DB check protects the object, never deletes it) → **grace period**
  (objects younger than N minutes are skipped — in-flight signed-URL
  downloads are protected; signed URLs live ≤ 900 s) → delete →
  object path + outcome logged (**never** secrets or signed URLs);
- bounded batches (one job dir per bucket per run at `limit=1` is the
  minimum; default 50) and **resumable** (re-run to process the next
  dir);
- a clip still being rendered/uploaded has a live `processing`/`pending`
  row (owner present) or is younger than the grace window — both are
  protected.

## Manual migration & cleanup runbook

Migrations are **repository-only and have NOT been applied to any
production database**. Apply in filename order (see
[ARCHITECTURE.md](./ARCHITECTURE.md) §Migrations for the full ordered
list and the TASK 015 subset):

```bash
# Development Supabase project, local CLI:
supabase db reset            # applies supabase/migrations/ in order
# or, for an existing dev project, the four TASK 015 files in order:
psql "$DATABASE_URL" -f supabase/migrations/20261127000000_task15_kick_source.sql
psql "$DATABASE_URL" -f supabase/migrations/20261128000000_task15_analysis_timeline.sql
psql "$DATABASE_URL" -f supabase/migrations/20261129000000_task15_generation_modes.sql
psql "$DATABASE_URL" -f supabase/migrations/20261130000000_task15_results_actions.sql
```

Ordering proof: `tests/database/task15-migration-order.test.ts` (PGlite,
real Postgres engine) applies all 15 migrations to an empty DB, upgrades
a TASK 014 end-state with only the four TASK 015 files, and re-applies
them (idempotent) — no static inspection claims.
