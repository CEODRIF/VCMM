/**
 * Phase 6 — artifact retention marker tests against a REAL PostgreSQL engine.
 *
 * Verifies the additive retention extension:
 *   - `jobs.purged_at` exists (timestamptz, nullable, no default)
 *   - a job reaching a TERMINAL state (completed or failed) gets
 *     `expires_at = now() + 30 days` (database-backed marker, not a
 *     runtime guess)
 *   - the marker is idempotent: non-status updates and re-firing never
 *     change an existing deadline; an operator-pre-set deadline wins
 *   - active/parked jobs stay deadline-less
 *   - `purged_at` is settable (the purge idempotency marker)
 *   - no new RLS policies (deny-by-default unchanged)
 *   - re-applying the migration is a no-op
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { PGlite } from "@electric-sql/pglite";

type Db = InstanceType<typeof PGlite>;
type Row = Record<string, unknown>;

let db: Db;

async function q(sql: string): Promise<Row[]> {
  return (await db.query(sql)).rows as Row[];
}

async function one(sql: string): Promise<Row | undefined> {
  const result = await db.query(sql);
  return (result.rows as Row[])[0];
}

async function scalar(sql: string): Promise<unknown> {
  const row = await one(sql);
  return row ? Object.values(row)[0] : null;
}

let seq = 0;

/** Seeds a session + a job in `queued` state, returns its id. */
async function seedQueuedJob(): Promise<string> {
  seq += 1;
  const sessionId = `11111111-1111-4111-8111-${String(111111111000 + seq).padStart(12, "0")}`;
  const jobId = `22222222-2222-4222-8222-${String(222222222000 + seq).padStart(12, "0")}`;
  await db.query(
    `insert into public.processing_sessions (id, token_hash, expires_at) values ('${sessionId}', 'p6-${seq}', now() + interval '1 hour')`
  );
  await db.query(
    `insert into public.jobs (id, session_id, source_type, source_path, requested_clips, clip_duration_seconds, status)
     values ('${jobId}', '${sessionId}', 'upload', 'sources/${jobId}/original/v.mp4', 1, 60, 'queued')`
  );
  return jobId;
}

/** Drives a queued job to `rendering` (active, locked) via legal transitions. */
async function driveToRendering(jobId: string): Promise<void> {
  await db.query(`update public.jobs set status = 'validating', locked_by = 'w', locked_at = now(), lease_expires_at = now() + interval '300 seconds' where id = '${jobId}'`);
  await db.query(`update public.jobs set status = 'ready', locked_by = null, locked_at = null, lease_expires_at = null where id = '${jobId}'`);
  await db.query(`update public.jobs set status = 'transcribing', locked_by = 'w', locked_at = now(), lease_expires_at = now() + interval '300 seconds' where id = '${jobId}'`);
  await db.query(`update public.jobs set status = 'analyzing', locked_by = 'w', locked_at = now(), lease_expires_at = now() + interval '300 seconds' where id = '${jobId}'`);
  await db.query(`update public.jobs set status = 'rendering' where id = '${jobId}'`);
}

const MIGRATIONS = [
  "20261113000000_initial_schema.sql",
  "20261114000000_phase2_job_settings_and_session_lookup.sql",
  "20261115000000_phase3_queue_and_ingestion.sql",
  "20261116000000_phase4_clip_analysis.sql",
  "20261117000000_phase5_rendering.sql",
  "20261120000000_phase5b_captions.sql",
  "20261123000000_phase6_results_retention.sql",
];
const MIGRATIONS_DIR = join(process.cwd(), "supabase", "migrations");

beforeAll(async () => {
  db = await PGlite.create();
  await db.exec(`
    create role anon noinherit nologin;
    create role service_role noinherit nologin bypassrls;
    grant usage on schema public to anon, service_role;
  `);
  for (const file of MIGRATIONS) {
    await db.exec(readFileSync(join(MIGRATIONS_DIR, file), "utf-8"));
  }
});

afterAll(async () => {
  await db?.close();
});

beforeEach(async () => {
  await db.query("delete from public.jobs");
});

function daysBetween(a: string, b: string): number {
  return (new Date(b).getTime() - new Date(a).getTime()) / 86_400_000;
}

describe("Phase 6 migration: retention markers", () => {
  it("adds a nullable purged_at column (no default)", async () => {
    const col = await one(
      `select data_type, is_nullable, column_default
       from information_schema.columns
       where table_schema = 'public' and table_name = 'jobs' and column_name = 'purged_at'`
    );
    expect(col).toBeDefined();
    expect(col!.data_type).toBe("timestamp with time zone");
    expect(col!.is_nullable).toBe("YES");
    expect(col!.column_default).toBeNull();
  });

  it("sets expires_at = now() + 30d when a job completes", async () => {
    const jobId = await seedQueuedJob();
    await driveToRendering(jobId);
    const before = await scalar(`select now()::text`);
    await db.query(
      `update public.jobs
       set status = 'completed', progress = 100, completed_at = now(),
           locked_by = null, locked_at = null, lease_expires_at = null
       where id = '${jobId}'`
    );
    const expiresAt = await scalar(`select expires_at::text from public.jobs where id = '${jobId}'`);
    expect(expiresAt).not.toBeNull();
    const drift = daysBetween(before as string, expiresAt as string);
    expect(drift).toBeGreaterThanOrEqual(29);
    expect(drift).toBeLessThanOrEqual(31);
  });

  it("sets expires_at when a job fails terminally", async () => {
    const jobId = await seedQueuedJob();
    await db.query(`update public.jobs set status = 'failed', last_error = 'x: y' where id = '${jobId}'`);
    const expiresAt = await scalar(`select expires_at::text from public.jobs where id = '${jobId}'`);
    expect(expiresAt).not.toBeNull();
  });

  it("leaves active jobs deadline-less", async () => {
    const jobId = await seedQueuedJob();
    const queued = await scalar(`select expires_at from public.jobs where id = '${jobId}'`);
    expect(queued).toBeNull();
    await driveToRendering(jobId);
    const rendering = await scalar(`select expires_at from public.jobs where id = '${jobId}'`);
    expect(rendering).toBeNull();
  });

  it("is idempotent: later updates never change the deadline", async () => {
    const jobId = await seedQueuedJob();
    await driveToRendering(jobId);
    await db.query(
      `update public.jobs set status = 'completed', completed_at = now(),
           locked_by = null, locked_at = null, lease_expires_at = null where id = '${jobId}'`
    );
    const first = await scalar(`select expires_at::text from public.jobs where id = '${jobId}'`);
    // progress update in the same state
    await db.query(`update public.jobs set progress = 100 where id = '${jobId}'`);
    const afterProgress = await scalar(`select expires_at::text from public.jobs where id = '${jobId}'`);
    expect(afterProgress).toBe(first);
  });

  it("never overrides an operator-pre-set deadline", async () => {
    const jobId = await seedQueuedJob();
    await db.query(`update public.jobs set expires_at = now() + interval '5 days' where id = '${jobId}'`);
    await db.query(`update public.jobs set status = 'failed' where id = '${jobId}'`);
    const drift = daysBetween(
      (await scalar(`select now()::text`)) as string,
      (await scalar(`select expires_at::text from public.jobs where id = '${jobId}'`)) as string
    );
    expect(drift).toBeGreaterThanOrEqual(4);
    expect(drift).toBeLessThanOrEqual(6);
  });

  it("purged_at is settable (purge idempotency marker)", async () => {
    const jobId = await seedQueuedJob();
    expect(await scalar(`select purged_at from public.jobs where id = '${jobId}'`)).toBeNull();
    await db.query(`update public.jobs set purged_at = now() where id = '${jobId}'`);
    expect(await scalar(`select purged_at is not null from public.jobs where id = '${jobId}'`)).toBe(true);
  });

  it("adds no RLS policies (deny-by-default unchanged)", async () => {
    const policies = await q(
      `select count(*) as n from pg_policies where schemaname = 'public' and tablename = 'jobs'`
    );
    expect(policies[0]?.n ?? 0).toBe(0);
    const rlsOn = await scalar(`select relrowsecurity from pg_class where relname = 'jobs'`);
    expect(rlsOn).toBe(true);
  });

  it("is idempotent (re-applying the migration is a no-op)", async () => {
    const sql = readFileSync(join(MIGRATIONS_DIR, "20261123000000_phase6_results_retention.sql"), "utf-8");
    await db.exec(sql);
    const count = await scalar(
      `select count(*) from information_schema.columns
       where table_schema = 'public' and table_name = 'jobs' and column_name = 'purged_at'`
    );
    expect(count).toBe(1);
  });
});
