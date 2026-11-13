/**
 * Phase 5B — caption payload column tests against a REAL PostgreSQL engine.
 *
 * Verifies the additive `clips.captions jsonb` extension:
 *   - the column exists, is nullable, defaults to NULL (Phase 1-5A rows are
 *     untouched and stay legal)
 *   - a word payload round-trips as an object with the documented shape
 *   - re-applying the migration is a no-op (idempotent `if not exists`)
 *   - RLS stays deny-by-default on `clips` (no new policies)
 *   - the Phase 5A `complete_job_rendering` guard is unaffected: clips with
 *     a captions payload complete exactly like clips without one (the guard
 *     checks status + storage_path, never the payload content)
 *   - no queue RPC signature or behavior from Phases 1-5A changed
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

/** Seeds a session + job parked at `analyzing` with one ready clip. */
async function seedParkedJob(): Promise<string> {
  seq += 1;
  const sessionId = `77777777-7777-4777-8777-${String(777777777000 + seq).padStart(12, "0")}`;
  const jobId = `88888888-8888-4888-8888-${String(888888888000 + seq).padStart(12, "0")}`;
  const clipId = `aaaaaaaa-aaaa-4aaa-8aaa-${String(500000000000 + seq).padStart(12, "0")}`;
  await db.query(
    `insert into public.processing_sessions (id, token_hash, expires_at) values ('${sessionId}', 'p5b-${seq}', now() + interval '1 hour')`
  );
  await db.query(
    `insert into public.jobs (id, session_id, source_type, source_path, requested_clips, status)
     values ('${jobId}', '${sessionId}', 'upload', 'sources/${jobId}/original/v.mp4', 1, 'queued')`
  );
  await db.query(`update public.jobs set status = 'validating', locked_by = 'w', locked_at = now(), lease_expires_at = now() + interval '300 seconds' where id = '${jobId}'`);
  await db.query(`update public.jobs set status = 'ready', locked_by = null, locked_at = null, lease_expires_at = null where id = '${jobId}'`);
  await db.query(`update public.jobs set status = 'transcribing', locked_by = 'w', locked_at = now(), lease_expires_at = now() + interval '300 seconds' where id = '${jobId}'`);
  await db.query(`update public.jobs set status = 'analyzing', locked_by = 'w', locked_at = now(), lease_expires_at = now() + interval '300 seconds' where id = '${jobId}'`);
  await db.query(`update public.jobs set status = 'rendering' where id = '${jobId}'`);
  await db.query(`update public.jobs set status = 'analyzing', locked_by = null, locked_at = null, lease_expires_at = null, progress = 100 where id = '${jobId}'`);
  await db.query(
    `insert into public.clips (id, job_id, title, start_time, end_time, sort_order, status, storage_path)
     values ('${clipId}', '${jobId}', 'clip', 60, 120, 0, 'ready', 'clips/${jobId}/${clipId}.mp4')`
  );
  return jobId;
}

const MIGRATIONS = [
  "20261113000000_initial_schema.sql",
  "20261114000000_phase2_job_settings_and_session_lookup.sql",
  "20261115000000_phase3_queue_and_ingestion.sql",
  "20261116000000_phase4_clip_analysis.sql",
  "20261117000000_phase5_rendering.sql",
  "20261120000000_phase5b_captions.sql",
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

  await db.exec(`
    grant select, insert, update, delete on public.processing_sessions, public.jobs, public.clips to anon, service_role;
  `);
});

afterAll(async () => {
  await db?.close();
});

beforeEach(async () => {
  // jobs.clips cascade — a clean queue makes the FIFO claim unambiguous.
  await db.query("delete from public.jobs");
});

describe("Phase 5B migration: clips.captions", () => {
  it("adds a nullable jsonb column defaulting to NULL", async () => {
    const col = await one(
      `select data_type, is_nullable, column_default
       from information_schema.columns
       where table_schema = 'public' and table_name = 'clips' and column_name = 'captions'`
    );
    expect(col).toBeDefined();
    expect(col!.data_type).toBe("jsonb");
    expect(col!.is_nullable).toBe("YES");
    expect(col!.column_default).toBeNull();
  });

  it("round-trips the documented word payload shape", async () => {
    const payload = {
      words: [
        { w: "hallo", s: 60.1, e: 60.7 },
        { w: "welt", s: 60.8, e: 61.5 },
      ],
    };
    const literal = `'${JSON.stringify(payload)}'::jsonb`;
    const jobId = await seedParkedJob();
    await db.query(`delete from public.clips where job_id = '${jobId}'`);
    await db.query(
      `insert into public.clips (id, job_id, title, start_time, end_time, status, storage_path, captions)
       values ('aaaaaaaa-aaaa-4aaa-8aaa-222222222222', '${jobId}', 'clip', 60, 120, 'ready',
               'clips/${jobId}/aaaaaaaa-aaaa-4aaa-8aaa-222222222222.mp4', ${literal})`
    );
    const row = await one(
      `select captions from public.clips where job_id = '${jobId}'`
    );
    expect(row!.captions).toEqual(payload);
  });

  it("keeps Phase 1-5A inserts legal (column omitted -> NULL)", async () => {
    const jobId = await seedParkedJob();
    const row = await one(`select captions from public.clips where job_id = '${jobId}'`);
    expect(row).toBeDefined();
    expect(row!.captions).toBeNull();
  });

  it("is idempotent (re-applying the migration is a no-op)", async () => {
    const sql = readFileSync(join(MIGRATIONS_DIR, "20261120000000_phase5b_captions.sql"), "utf-8");
    await db.exec(sql); // must not raise
    const count = await scalar(
      `select count(*) from information_schema.columns
       where table_schema = 'public' and table_name = 'clips' and column_name = 'captions'`
    );
    expect(count).toBe(1);
  });

  it("leaves clips RLS deny-by-default (no policies added)", async () => {
    const rlsOn = await scalar(
      `select relrowsecurity from pg_class where relname = 'clips'`
    );
    expect(rlsOn).toBe(true);
    const policies = await q(
      `select count(*) as n from pg_policies where schemaname = 'public' and tablename = 'clips'`
    );
    expect(policies[0]?.n ?? 0).toBe(0);
  });
});

describe("Phase 5B does not regress the Phase 5A completion guard", () => {
  it("completes a rendering job whose clips carry captions payloads", async () => {
    const jobId = await seedParkedJob();
    // attach a payload to the ready clip
    await db.query(
      `update public.clips
       set captions = '{"words":[{"w":"hallo","s":60.1,"e":60.7}]}'::jsonb
       where job_id = '${jobId}'`
    );
    // parked -> claim -> rendering
    const claimed = await one(`select * from public.claim_next_job('w5b', 300)`);
    expect(claimed!.id).toBe(jobId);
    expect(claimed!.status).toBe("rendering");
    const done = await scalar(`select public.complete_job_rendering('${jobId}', 'w5b')`);
    expect(done).toBe(true);
    const status = await scalar(`select status from public.jobs where id = '${jobId}'`);
    expect(status).toBe("completed");
  });

  it("still refuses completion when a clip is not ready (payload or not)", async () => {
    const jobId = await seedParkedJob();
    await db.query(`update public.clips set status = 'pending', captions = null where job_id = '${jobId}'`);
    const claimed = await one(`select * from public.claim_next_job('w5b2', 300)`);
    expect(claimed!.id).toBe(jobId);
    const done = await scalar(`select public.complete_job_rendering('${jobId}', 'w5b2')`);
    expect(done).toBe(false);
    const status = await scalar(`select status from public.jobs where id = '${jobId}'`);
    expect(status).toBe("rendering");
  });
});
