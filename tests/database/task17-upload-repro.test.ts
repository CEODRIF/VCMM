/**
 * TASK 017 — production upload-failure investigation (evidence, DB level).
 *
 * Reproduces the EXACT database sequence of the reported incident on the
 * real engine (PGlite, Postgres 16 wasm), using the same SQL shapes as the
 * real data-access layer (src/lib/db/jobs.ts):
 *
 *   1. "Start Creating" #1 → insert job1 ('uploading')
 *      → authorization UPDATE (server-generated path + TTL)
 *      → the 250 MB browser→storage PUT fails (no database write at all —
 *        a failed transfer is invisible to Postgres, which is why job1
 *        sits in 'uploading' forever: the zombie the user saw)
 *   2. "Start Creating" #2 → insert job2 + authorization UPDATE
 *      → proves repeated submission is DB-safe (no constraint error —
 *        the "Internal server error." was NOT a job-state rejection)
 *   3. the worker's reaper fails the STALE zombie job1 terminally (which
 *      arms retention); job2 is untouched and can still be finalized.
 *
 * This isolates the storage-layer failure from the database: the job
 * records are always consistent; the incident's opacity came from the
 * transfer result being uninterpreted (now: describeStorageTransferFailure)
 * and from masked 500s (now: typed 502/503 with explicit outcomes).
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { PGlite } from "@electric-sql/pglite";

const MIGRATIONS_DIR = join(process.cwd(), "supabase", "migrations");
const ALL_MIGRATIONS = readdirSync(MIGRATIONS_DIR).filter((f) => f.endsWith(".sql")).sort();

const SESSION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const JOB1 = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"; // first "Start Creating" (stale)
const JOB2 = "ddddeeee-dddd-4ddd-8ddd-dddddddddddd"; // second "Start Creating" (fresh)
const OBJ1 = "aaaa0000-0000-4000-8000-000000000001.mp4";
const OBJ2 = "aaaa0000-0000-4000-8000-000000000002.mp4";

async function bootstrap(): Promise<PGlite> {
  const db = await PGlite.create();
  await db.exec(`
    create role anon noinherit nologin;
    create role service_role noinherit nologin bypassrls;
    grant usage on schema public to anon, service_role;
  `);
  await db.exec(`
    create schema if not exists storage;
    create table if not exists storage.buckets (
        id uuid primary key,
        name text not null,
        owner uuid,
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now(),
        public boolean not null default false,
        avif_conversion boolean,
        file_size_limit bigint,
        allowed_mime_types text[]
    );
    create table if not exists storage.objects (
        id uuid primary key,
        bucket_id uuid,
        name text,
        owner uuid
    );
    alter table storage.objects enable row level security;
  `);
  return db;
}

async function apply(db: PGlite, files: string[]): Promise<void> {
  for (const file of files) {
    await db.exec(readFileSync(join(MIGRATIONS_DIR, file), "utf-8"));
  }
}

async function q(db: PGlite, sql: string): Promise<Array<Record<string, unknown>>> {
  const res = await db.query(sql);
  return res?.rows as Array<Record<string, unknown>>;
}

/** insertJob shape for an upload job (src/lib/db/jobs.ts). */
async function insertUploadJob(db: PGlite, id: string, createdAt: string): Promise<void> {
  await q(db, `
    insert into public.jobs
      (id, session_id, source_type, source_path, generation_mode, requested_clips,
       status, progress, created_at)
    values
      ('${id}', '${SESSION_ID}', 'upload', 'sources/${id}/reserved',
       'highlights', 3, 'uploading', 0, ${createdAt})`);
}

/** setUploadAuthorization shape (guarded update; returns rows touched). */
async function setAuthorization(
  db: PGlite,
  id: string,
  generatedName: string
): Promise<number> {
  const res = await db.query(
    `update public.jobs
        set source_path = 'sources/${id}/original/${generatedName}',
            upload_auth_expires_at = now() + interval '15 minutes',
            source_metadata = '{"original_file_name": "lecture.mp4", "declared_size_bytes": 262144000, "declared_mime_type": "video/mp4"}'::jsonb
      where id = '${id}' and session_id = '${SESSION_ID}' and status = 'uploading'
      returning id`
  );
  // PGlite exposes the affected-row count as `affectedRows`.
  return res?.affectedRows ?? 0;
}

/** markUploadComplete shape (guarded update; returns rows touched). */
async function markComplete(db: PGlite, id: string): Promise<number> {
  const res = await db.query(
    `update public.jobs
        set status = 'queued', upload_completed_at = now()
      where id = '${id}' and session_id = '${SESSION_ID}' and status = 'uploading'
      returning id`
  );
  return res?.affectedRows ?? 0;
}

describe("TASK 017 — incident reproduction at the database level", () => {
  let db: PGlite;

  beforeAll(async () => {
    db = await bootstrap();
    await apply(db, ALL_MIGRATIONS);
    await q(db, `insert into public.processing_sessions (id, token_hash, expires_at)
                 values ('${SESSION_ID}', 'hash', now() + interval '1 hour')`);
  });
  afterAll(async () => {
    await db?.close();
  });

  it("step 1 — first 'Start Creating': job1 inserted + authorized", async () => {
    await insertUploadJob(db, JOB1, "now() - interval '2 hours'");
    const touched = await setAuthorization(db, JOB1, OBJ1);
    expect(touched).toBe(1);
    const [row] = await q(
      db,
      `select status, source_path, upload_auth_expires_at, source_metadata
         from public.jobs where id = '${JOB1}'`
    );
    expect(row?.status).toBe("uploading");
    expect(row?.source_path).toBe(`sources/${JOB1}/original/${OBJ1}`);
    expect(row?.upload_auth_expires_at).not.toBeNull();
    expect((row?.source_metadata as Record<string, unknown>)?.declared_size_bytes).toBe(262_144_000);
  });

  it("step 2 — the failed 250 MB PUT is invisible to the DB: job1 is the zombie the user saw", async () => {
    // A failed transfer performs NO database write (the PUT goes straight
    // to storage). The row stays 'uploading' with no retention deadline —
    // exactly the 'Job … is waiting for its file' state.
    const [row] = await q(
      db,
      `select status, expires_at, locked_by, lease_expires_at from public.jobs where id = '${JOB1}'`
    );
    expect(row?.status).toBe("uploading");
    expect(row?.expires_at).toBeNull(); // never went terminal → no retention
    expect(row?.locked_by).toBeNull(); // the worker never claimed it
  });

  it("step 3 — second 'Start Creating': a fresh job2, no constraint error", async () => {
    // The reported 'Internal server error.' CANNOT come from job state:
    // repeated creation is fully accepted at the database level.
    await insertUploadJob(db, JOB2, "now()");
    const touched = await setAuthorization(db, JOB2, OBJ2);
    expect(touched).toBe(1);
    const rows = await q(db, `select count(*)::int as n from public.jobs where session_id = '${SESSION_ID}'`);
    expect(Number(rows[0]?.n)).toBe(2);
    const [row1, row2] = await q(
      db,
      `select status from public.jobs where id in ('${JOB1}', '${JOB2}') order by created_at`
    );
    expect(row1?.status).toBe("uploading");
    expect(row2?.status).toBe("uploading");
  });

  it("step 4 — the worker's reaper fails the stale zombie job1 (and only it)", async () => {
    const reaped = await q(db, `select public.reap_stale_uploading_jobs(1080) as n`);
    expect(Number(reaped[0]?.n)).toBe(1);
    const [row1] = await q(
      db,
      `select status, last_error, expires_at from public.jobs where id = '${JOB1}'`
    );
    expect(row1?.status).toBe("failed");
    expect(String(row1?.last_error)).toMatch(/did not finish in time/i);
    expect(row1?.expires_at).not.toBeNull(); // retention armed
    const [row2] = await q(db, `select status from public.jobs where id = '${JOB2}'`);
    expect(row2?.status).toBe("uploading"); // fresh job untouched
  });

  it("step 5 — the retried upload (job2) still finalizes normally", async () => {
    const touched = await markComplete(db, JOB2);
    expect(touched).toBe(1);
    const [row] = await q(
      db,
      `select status, upload_completed_at from public.jobs where id = '${JOB2}'`
    );
    expect(row?.status).toBe("queued");
    expect(row?.upload_completed_at).not.toBeNull();
    // A second (accidental) finalization cannot re-touch it.
    expect(await markComplete(db, JOB2)).toBe(0);
  });
});
