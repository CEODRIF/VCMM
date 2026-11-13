/**
 * Phase 7 (TASK 007) — stream-intelligence schema tests against a REAL
 * PostgreSQL engine (PGlite WASM).
 *
 * Verifies the additive migration `20261124000000`:
 *   - `processing_checkpoints` (PK (job_id, chunk_index), status='done'
 *     only, positive times, FK cascade, RLS deny-by-default, no policies)
 *   - `candidate_events` (PK (job_id, event_key) → exact retry dedup,
 *     score/confidence bounds, evidence jsonb, FK cascade, RLS)
 *   - `jobs.source_type` now accepts 'twitch' WITH source_url (additive:
 *     youtube/upload still validate; twitch without a URL is rejected)
 *   - deleting a job cascades both new tables
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

/** Seeds a session + a job of the given source type, returns its id. */
async function seedJob(sourceType = "upload"): Promise<string> {
  seq += 1;
  const sessionId = `11111111-1111-4111-8111-${String(111111111000 + seq).padStart(12, "0")}`;
  const jobId = `22222222-2222-4222-8222-${String(222222222000 + seq).padStart(12, "0")}`;
  await db.query(
    `insert into public.processing_sessions (id, token_hash, expires_at) values ('${sessionId}', 'p7-${seq}', now() + interval '1 hour')`
  );
  const sourceCol = sourceType === "upload" ? "source_path" : "source_url";
  const sourceVal = sourceType === "upload" ? `sources/${jobId}/original/v.mp4` : "https://www.youtube.com/watch?v=xyz";
  await db.query(
    `insert into public.jobs (id, session_id, source_type, ${sourceCol}, requested_clips, clip_duration_seconds, status)
     values ('${jobId}', '${sessionId}', '${sourceType}', '${sourceVal}', 1, 60, 'queued')`
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
  "20261123000000_phase6_results_retention.sql",
  "20261124000000_phase7_stream_intelligence.sql",
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

describe("Phase 7 migration: processing_checkpoints", () => {
  it("exists with the resume contract (PK (job_id, chunk_index), done-only status, positive times)", async () => {
    const cols = await q(
      `select column_name, is_nullable from information_schema.columns
       where table_schema = 'public' and table_name = 'processing_checkpoints'
       order by column_name`
    );
    const names = cols.map((c) => c.column_name as string);
    for (const expected of ["job_id", "chunk_index", "start_time", "end_time", "status", "features", "updated_at"]) {
      expect(names).toContain(expected);
    }

    const jobId = await seedJob();
    await db.query(
      `insert into public.processing_checkpoints (job_id, chunk_index, start_time, end_time, features)
       values ('${jobId}', 0, 0, 600, '{"rms":[1,2]}')`
    );

    // PK: same (job_id, chunk_index) cannot be inserted twice.
    await expect(
      db.query(
        `insert into public.processing_checkpoints (job_id, chunk_index, start_time, end_time, features)
         values ('${jobId}', 0, 0, 600, '{}')`
      )
    ).rejects.toThrow();

    // status is constrained to 'done' only.
    await expect(
      db.query(
        `insert into public.processing_checkpoints (job_id, chunk_index, start_time, end_time, status, features)
         values ('${jobId}', 1, 600, 1200, 'pending', '{}')`
      )
    ).rejects.toThrow();

    // end_time must be > start_time; chunk_index >= 0.
    await expect(
      db.query(
        `insert into public.processing_checkpoints (job_id, chunk_index, start_time, end_time, features)
         values ('${jobId}', 2, 500, 500, '{}')`
      )
    ).rejects.toThrow();
    await expect(
      db.query(
        `insert into public.processing_checkpoints (job_id, chunk_index, start_time, end_time, features)
         values ('${jobId}', -1, 0, 10, '{}')`
      )
    ).rejects.toThrow();

    // RLS deny-by-default, no policies.
    expect(await scalar(`select relrowsecurity from pg_class where relname = 'processing_checkpoints'`)).toBe(true);
    const policies = await q(
      `select count(*) as n from pg_policies where schemaname = 'public' and tablename = 'processing_checkpoints'`
    );
    expect(policies[0]?.n ?? 0).toBe(0);
  });

  it("cascades when the job is deleted", async () => {
    const jobId = await seedJob();
    await db.query(
      `insert into public.processing_checkpoints (job_id, chunk_index, start_time, end_time, features)
       values ('${jobId}', 0, 0, 600, '{}')`
    );
    await db.query(`delete from public.jobs where id = '${jobId}'`);
    expect(await scalar(`select count(*) from public.processing_checkpoints`)).toBe(0);
  });
});

describe("Phase 7 migration: candidate_events", () => {
  it("exact-dedupes on (job_id, event_key) — the retry contract", async () => {
    const jobId = await seedJob();
    const insert = `insert into public.candidate_events (job_id, event_key, start_time, end_time, evidence)
       values ('${jobId}', 'evt-1', 100.5, 109.5, '{"audio": {"strength": 0.9}}')`;
    await db.query(insert);
    // Re-running the same detection must not duplicate: PK on (job_id, event_key).
    await expect(db.query(insert)).rejects.toThrow();
    // An UPDATE (upsert semantics) is the idempotent re-write.
    await db.query(
      `insert into public.candidate_events (job_id, event_key, start_time, end_time, score, evidence)
       values ('${jobId}', 'evt-1', 100.5, 109.5, 82, '{"audio": {"strength": 0.9}, "visual": {"scene": 0.7}}')
       on conflict (job_id, event_key) do update
         set score = excluded.score, evidence = excluded.evidence`
    );
    expect(await scalar(`select count(*) from public.candidate_events where job_id = '${jobId}'`)).toBe(1);
    expect(Number(await scalar(`select score from public.candidate_events where event_key = 'evt-1'`))).toBe(82);
  });

  it("bounds score (0-100) and confidence (0-1)", async () => {
    const jobId = await seedJob();
    await expect(
      db.query(
        `insert into public.candidate_events (job_id, event_key, start_time, end_time, score, evidence)
         values ('${jobId}', 'bad-score', 0, 10, 101, '{}')`
      )
    ).rejects.toThrow();
    await expect(
      db.query(
        `insert into public.candidate_events (job_id, event_key, start_time, end_time, confidence, evidence)
         values ('${jobId}', 'bad-conf', 0, 10, 1.5, '{}')`
      )
    ).rejects.toThrow();
    expect(await scalar(`select relrowsecurity from pg_class where relname = 'candidate_events'`)).toBe(true);
    const policies = await q(
      `select count(*) as n from pg_policies where schemaname = 'public' and tablename = 'candidate_events'`
    );
    expect(policies[0]?.n ?? 0).toBe(0);
  });

  it("keeps events of different jobs separate (ownership-safe)", async () => {
    const jobA = await seedJob();
    const jobB = await seedJob();
    await db.query(
      `insert into public.candidate_events (job_id, event_key, start_time, end_time, evidence)
       values ('${jobA}', 'evt-1', 0, 5, '{}')`
    );
    // The SAME event_key under a different job is a DIFFERENT row (PK includes job_id).
    await db.query(
      `insert into public.candidate_events (job_id, event_key, start_time, end_time, evidence)
       values ('${jobB}', 'evt-1', 0, 5, '{}')`
    );
    expect(await scalar(`select count(*) from public.candidate_events`)).toBe(2);
    await db.query(`delete from public.jobs where id = '${jobA}'`);
    expect(await scalar(`select count(*) from public.candidate_events where job_id = '${jobB}'`)).toBe(1);
  });
});

describe("Phase 7 migration: jobs.source_type += 'twitch'", () => {
  it("accepts a twitch job with a VOD URL", async () => {
    const jobId = await seedJob("twitch");
    expect(jobId).toBeDefined();
    expect(await scalar(`select source_type from public.jobs where id = '${jobId}'`)).toBe("twitch");
  });

  it("still accepts youtube and upload jobs (additive, not breaking)", async () => {
    expect(await scalar(`select source_type from public.jobs where id = '${await seedJob("upload")}'`)).toBe("upload");
    expect(await scalar(`select source_type from public.jobs where id = '${await seedJob("youtube")}'`)).toBe("youtube");
  });

  it("rejects a twitch job WITHOUT a source_url (source-presence check extended)", async () => {
    const sessionId = "11111111-1111-4111-8111-ffffffffffff";
    const jobId = "22222222-2222-4222-8222-ffffffffffff";
    await db.query(
      `insert into public.processing_sessions (id, token_hash, expires_at) values ('${sessionId}', 'p7-notwitch', now() + interval '1 hour')`
    );
    await expect(
      db.query(
        `insert into public.jobs (id, session_id, source_type, requested_clips, clip_duration_seconds, status)
         values ('${jobId}', '${sessionId}', 'twitch', 1, 60, 'queued')`
      )
    ).rejects.toThrow();
  });
});

describe("Phase 7 migration: idempotency", () => {
  it("re-applying the migration is a no-op", async () => {
    const sql = readFileSync(
      join(MIGRATIONS_DIR, "20261124000000_phase7_stream_intelligence.sql"),
      "utf-8"
    );
    await db.exec(sql);
    expect(
      await scalar(
        `select count(*) from information_schema.tables where table_schema = 'public' and table_name = 'candidate_events'`
      )
    ).toBe(1);
    // A twitch job still works after the constraint re-creation.
    const jobId = await seedJob("twitch");
    expect(await scalar(`select source_type from public.jobs where id = '${jobId}'`)).toBe("twitch");
  });
});
