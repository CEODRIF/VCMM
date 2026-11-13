/**
 * Database + RLS tests against a REAL PostgreSQL engine.
 *
 * Uses @electric-sql/pglite (Postgres 16 compiled to WebAssembly, in-memory)
 * so the exact migration files are executed and Row Level Security behavior
 * is verified without any external service or cloud project.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { PGlite } from "@electric-sql/pglite";

type Db = InstanceType<typeof PGlite>;
type Row = Record<string, unknown>;

let db: Db;

async function rows(sql: string): Promise<Row[]> {
  const result = await db.query(sql);
  return result.rows as Row[];
}

async function singleValue(sql: string): Promise<unknown> {
  const result = await db.query(sql);
  const row = (result.rows as Row[])[0];
  return row ? Object.values(row)[0] : null;
}

async function expectSqlError(sql: string, pattern: RegExp) {
  let error: unknown = null;
  try {
    await db.query(sql);
  } catch (e) {
    error = e;
  }
  expect(error, `expected SQL error for: ${sql}`).not.toBeNull();
  expect(String(error)).toMatch(pattern);
}

beforeAll(async () => {
  db = await PGlite.create();

  // Mirror the Supabase role model: the roles must exist before the Phase 3
  // migration (it grants EXECUTE on its queue RPCs to service_role), while
  // the table grants must come after the migrations create the tables.
  //  - `anon`: the anonymous client role (subject to RLS, no bypass)
  //  - `service_role`: backend role with BYPASSRLS (used by the API server)
  await db.exec(`
    create role anon noinherit nologin;
    create role service_role noinherit nologin bypassrls;
    grant usage on schema public to anon, service_role;
  `);

  const migrationsDir = join(process.cwd(), "supabase", "migrations");
  const files = [
    "20261113000000_initial_schema.sql",
    "20261114000000_phase2_job_settings_and_session_lookup.sql",
    "20261115000000_phase3_queue_and_ingestion.sql",
  ];
  for (const file of files) {
    await db.exec(readFileSync(join(migrationsDir, file), "utf-8"));
  }

  await db.exec(`
    grant select, insert, update, delete on public.processing_sessions, public.jobs, public.clips to anon, service_role;
  `);
});

afterAll(async () => {
  await db?.close();
});

describe("migrations apply cleanly to a real Postgres engine", () => {
  it("creates all three tables", async () => {
    const result = await rows(
      "select table_name from information_schema.tables where table_schema = 'public' order by 1"
    );
    const tables = result.map((r) => r.table_name as string);
    expect(tables).toContain("processing_sessions");
    expect(tables).toContain("jobs");
    expect(tables).toContain("clips");
  });

  it("adds the Phase 2 columns and session lookup index", async () => {
    const columns = await rows(
      "select column_name from information_schema.columns where table_name = 'jobs' order by 1"
    );
    const names = columns.map((c) => c.column_name as string);
    expect(names).toContain("clip_duration_seconds");
    expect(names).toContain("captions_enabled");

    const index = await rows(
      "select indexname from pg_indexes where tablename = 'processing_sessions' and indexname = 'sessions_token_hash_idx'"
    );
    expect(index).toHaveLength(1);
  });

  it("keeps Phase 1 intact (no duplicated tables, RLS still enabled)", async () => {
    const rls = await rows(
      "select relname, relrowsecurity from pg_class where relname in ('processing_sessions','jobs','clips') order by 1"
    );
    expect(rls).toHaveLength(3);
    for (const row of rls) {
      expect(row.relrowsecurity as boolean).toBeTruthy();
    }
  });
});

describe("schema constraints", () => {
  let sessionId = "";

  beforeAll(async () => {
    sessionId = (await singleValue(
      "insert into public.processing_sessions (token_hash, expires_at) values ('h1', now() + interval '1 hour') returning id::text"
    )) as string;
  });

  it("enforces session expiry in the future", async () => {
    await expectSqlError(
      "insert into public.processing_sessions (token_hash, created_at, expires_at) values ('h2', now(), now() - interval '1 hour')",
      /check|expiry/i
    );
  });

  it("requires a source for youtube jobs (source_url)", async () => {
    await expectSqlError(
      `insert into public.jobs (session_id, source_type, requested_clips) values ('${sessionId}', 'youtube', 1)`,
      /jobs_source_present|check/i
    );
  });

  it("requires a source path for upload jobs", async () => {
    await expectSqlError(
      `insert into public.jobs (session_id, source_type, source_path, requested_clips) values ('${sessionId}', 'upload', null, 1)`,
      /jobs_source_present|not null|check/i
    );
  });

  it("enforces the 60-180s clip duration on jobs", async () => {
    await expectSqlError(
      `insert into public.jobs (session_id, source_type, source_url, requested_clips, clip_duration_seconds) values ('${sessionId}', 'youtube', 'https://youtube.com/watch?v=x', 1, 59)`,
      /jobs_clip_duration_bounds|check/i
    );
    await expectSqlError(
      `insert into public.jobs (session_id, source_type, source_url, requested_clips, clip_duration_seconds) values ('${sessionId}', 'youtube', 'https://youtube.com/watch?v=x', 1, 181)`,
      /jobs_clip_duration_bounds|check/i
    );
  });

  it("enforces the job state machine values", async () => {
    await expectSqlError(
      `insert into public.jobs (session_id, source_type, source_url, requested_clips, status) values ('${sessionId}', 'youtube', 'https://youtube.com/watch?v=x', 1, 'exploding')`,
      /check/i
    );
  });

  it("derives clip duration and enforces 60-180s bounds", async () => {
    const jobId = (await singleValue(
      `insert into public.jobs (session_id, source_type, source_url, requested_clips) values ('${sessionId}', 'youtube', 'https://youtube.com/watch?v=x', 2) returning id::text`
    )) as string;

    const duration = await singleValue(
      `insert into public.clips (job_id, title, start_time, end_time) values ('${jobId}', 'clip one', 0, 60) returning duration::float`
    );
    expect(duration).toBe(60);

    await expectSqlError(
      `insert into public.clips (job_id, title, start_time, end_time) values ('${jobId}', 'too short', 0, 59)`,
      /clips_duration_bounds|check/i
    );
    await expectSqlError(
      `insert into public.clips (job_id, title, start_time, end_time) values ('${jobId}', 'too long', 0, 181)`,
      /clips_duration_bounds|check/i
    );
    await expectSqlError(
      `insert into public.clips (job_id, title, start_time, end_time) values ('${jobId}', 'backwards', 100, 100)`,
      /clips_end_after_start|check/i
    );
  });
});

describe("row level security (deny by default)", () => {
  it("seeds a row as service_role", async () => {
    const id = (await singleValue(
      "insert into public.processing_sessions (token_hash, expires_at) values ('rls-seed', now() + interval '1 hour') returning id::text"
    )) as string;
    await db.query(
      `insert into public.jobs (session_id, source_type, source_url, requested_clips, clip_duration_seconds) values ('${id}', 'youtube', 'https://youtube.com/watch?v=rls', 1, 60)`
    );
  });

  it("hides all rows from anon (select returns nothing)", async () => {
    await db.query("set role anon");
    const sessions = await singleValue("select count(*)::int as n from public.processing_sessions");
    const jobs = await singleValue("select count(*)::int as n from public.jobs");
    const clips = await singleValue("select count(*)::int as n from public.clips");
    expect(sessions).toBe(0);
    expect(jobs).toBe(0);
    expect(clips).toBe(0);
    await db.query("set role postgres");
  });

  it("blocks anon writes on every table", async () => {
    await db.query("set role anon");
    // INSERT is rejected with an explicit RLS error…
    await expectSqlError(
      "insert into public.processing_sessions (token_hash, expires_at) values ('anon-attempt', now() + interval '1 hour')",
      /row-level security/i
    );
    // …UPDATE/DELETE under deny-by-default RLS silently affect zero rows.
    const update = await db.query("update public.jobs set progress = 50");
    expect(update.affectedRows).toBe(0);
    const clipDelete = await db.query("delete from public.clips");
    expect(clipDelete.affectedRows).toBe(0);
    await db.query("set role postgres");

    // Verify nothing actually changed.
    const touched = await singleValue("select count(*)::int as n from public.jobs where progress = 50");
    expect(touched).toBe(0);
    const clipsLeft = await singleValue("select count(*)::int as n from public.clips");
    expect(clipsLeft).toBeGreaterThanOrEqual(1);
  });

  it("allows service_role full visibility (BYPASSRLS)", async () => {
    await db.query("set role service_role");
    const jobs = await singleValue("select count(*)::int as n from public.jobs");
    expect(jobs).toBeGreaterThanOrEqual(2);
  });
});
