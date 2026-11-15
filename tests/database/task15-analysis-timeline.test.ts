/**
 * TASK 015 — full-timeline coverage schema test against a REAL PostgreSQL
 * engine (PGlite WASM).
 *
 * Verifies the repository-only migration `20261128000000_task15_analysis_timeline.sql`:
 * the analysis_timeline table (one row per sequential window over
 * [0, duration]), its honesty-bound columns (event_importance / confidence
 * ranges), idempotent (job_id, window_index) upserts, cascade delete with
 * the job, and RLS deny-by-default for the anon role (service-role-only
 * access).
 *
 * REPOSITORY-ONLY: no production database is touched.
 */
import { beforeAll, afterAll, beforeEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { PGlite } from "@electric-sql/pglite";

let db: PGlite | undefined;

const MIGRATIONS_DIR = join(process.cwd(), "supabase", "migrations");
const MIGRATIONS = [
  "20261113000000_initial_schema.sql",
  "20261114000000_phase2_job_settings_and_session_lookup.sql",
  "20261115000000_phase3_queue_and_ingestion.sql",
  "20261116000000_phase4_clip_analysis.sql",
  "20261117000000_phase5_rendering.sql",
  "20261120000000_phase5b_captions.sql",
  "20261123000000_phase6_results_retention.sql",
  "20261124000000_phase7_stream_intelligence.sql",
  "20261125000000_phase7b_intel_integration.sql",
  "20261126000000_task14_universal_link_source.sql",
  "20261127000000_task15_kick_source.sql",
  "20261128000000_task15_analysis_timeline.sql",
];

const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const OTHER_JOB_ID = "33333333-3333-4333-8333-333333333333";

type Row = Record<string, unknown>;

async function q(sql: string): Promise<Row[]> {
  const result = await db!.query(sql);
  return result.rows as Row[];
}

beforeAll(async () => {
  db = await PGlite.create();
  await db.exec(`
    create role anon noinherit nologin;
    create role service_role noinherit nologin bypassrls;
    grant usage on schema public to anon, service_role;
  `);
  for (const f of MIGRATIONS) {
    await db.exec(readFileSync(join(MIGRATIONS_DIR, f), "utf-8"));
  }
  // Mirror Supabase's grant surface: the service role can reach the tables
  // (bypassrls only bypasses RLS, not table privileges); anon gets schema
  // usage but NO table privileges + RLS deny-by-default = no access.
  await db.exec(`grant select, insert, update, delete on all tables in schema public to service_role;`);
});

afterAll(async () => {
  await db?.close();
});

beforeEach(async () => {
  // Clean slate per test (as the default superuser role).
  await db!.exec(`reset role`);
  await db!.exec(`delete from public.analysis_timeline; delete from public.jobs;`);
});

async function seedJob(jobId: string): Promise<void> {
  // One shared session row (id = SESSION_ID, matching the jobs FK target).
  await q(`insert into public.processing_sessions (id, token_hash, expires_at)
           values ('${SESSION_ID}', 't', now() + interval '1 hour') on conflict do nothing`);
  await q(`insert into public.jobs
           (id, session_id, source_type, source_path, requested_clips,
            clip_duration_seconds, status)
           values ('${jobId}', '${SESSION_ID}', 'upload',
                   'sources/${jobId}/original/generated.mp4', 1, 60, 'queued')`);
}

const DEFAULT_ASSESSMENT_SQL =
  `'{"status":"ok","scene_description":"s","event_type":"combat","event_importance":0.8,"candidate_clip":true,"reasoning":"r"}'::jsonb`;

function insertRow(
  jobId: string,
  windowIndex: number,
  start: number,
  end: number,
  assessmentSql: string = DEFAULT_ASSESSMENT_SQL
): Promise<Row[]> {
  return q(`insert into public.analysis_timeline
             (job_id, window_index, start_time, end_time, event_type, transcript,
              scene_description, visual_evidence, audio_evidence, event_importance,
              candidate_clip, confidence, gemini_assessment)
             values ('${jobId}', ${windowIndex}, ${start}, ${end}, 'combat',
                     'actual words', 'a scene', '{}'::jsonb, '{}'::jsonb, 0.8,
                     true, 0.7, ${assessmentSql})
             returning *`);
}

describe("TASK 015 migration: analysis_timeline", () => {
  it("creates the coverage table with the honesty-bound columns", async () => {
    await seedJob(JOB_ID);
    const cols = await q(`select column_name from information_schema.columns
                          where table_name = 'analysis_timeline' order by column_name`);
    expect(cols.map((c) => c.column_name).sort()).toEqual([
      "audio_evidence", "candidate_clip", "confidence", "created_at",
      "end_time", "event_importance", "event_type", "gemini_assessment",
      "job_id", "scene_description", "start_time", "transcript", "visual_evidence",
      "window_index",
    ]);
    const rls = await q(`select relname, relrowsecurity from pg_class where relname = 'analysis_timeline'`);
    expect(rls[0].relrowsecurity).toBe(true);
    // No policy for anon: deny-by-default (same contract as candidate_events).
    const policies = await q(`select count(*)::int as n from pg_policies where tablename = 'analysis_timeline'`);
    expect(policies[0].n).toBe(0);
  });

  it("persists window rows and upserts idempotently on (job_id, window_index)", async () => {
    await seedJob(JOB_ID);
    await insertRow(JOB_ID, 0, 0, 10);
    await insertRow(JOB_ID, 1, 10, 20);
    await insertRow(JOB_ID, 2, 20, 25); // the explicit remainder window
    let rows = await q(`select window_index, end_time from public.analysis_timeline
                        where job_id = '${JOB_ID}' order by window_index`);
    expect(rows).toHaveLength(3);
    expect(rows[2].end_time).toBe(25);

    // Re-upsert window 1 (resume semantics: the same key is replaced, not
    // duplicated) — with an UPDATED honest assessment.
    await q(`insert into public.analysis_timeline
              (job_id, window_index, start_time, end_time, event_type, transcript,
               scene_description, visual_evidence, audio_evidence, event_importance,
               candidate_clip, confidence, gemini_assessment)
              values ('${JOB_ID}', 1, 10, 20, 'combat', 'actual words v2', 'a scene',
                      '{}'::jsonb, '{}'::jsonb, 0.8, true, 0.7,
                      '{"status":"ok","scene_description":"updated"}'::jsonb)
              on conflict (job_id, window_index) do update set transcript = excluded.transcript,
                gemini_assessment = excluded.gemini_assessment`);
    rows = await q(`select transcript, gemini_assessment from public.analysis_timeline
                    where job_id = '${JOB_ID}' and window_index = 1`);
    expect(rows).toHaveLength(1);
    expect(rows[0].transcript).toBe("actual words v2");
    expect((rows[0].gemini_assessment as { scene_description: string }).scene_description).toBe("updated");
  });

  it("enforces the honesty-bound checks (ranges + ordering)", async () => {
    await seedJob(JOB_ID);
    // end < start is impossible.
    await expect(q(`insert into public.analysis_timeline
                    (job_id, window_index, start_time, end_time)
                    values ('${JOB_ID}', 5, 20, 10)`)).rejects.toThrow();
    // importance/confidence are bounded to [0, 1] (or NULL = not measured).
    await expect(q(`insert into public.analysis_timeline
                    (job_id, window_index, start_time, end_time, event_importance)
                    values ('${JOB_ID}', 6, 0, 1, 1.5)`)).rejects.toThrow();
    await expect(q(`insert into public.analysis_timeline
                    (job_id, window_index, start_time, end_time, confidence)
                    values ('${JOB_ID}', 7, 0, 1, -0.1)`)).rejects.toThrow();
    // NULL importance is the honest 'not measured' state — allowed.
    await q(`insert into public.analysis_timeline
             (job_id, window_index, start_time, end_time, event_importance)
             values ('${JOB_ID}', 8, 0, 1, null)`);
    // negative window_index is impossible.
    await expect(q(`insert into public.analysis_timeline
                    (job_id, window_index, start_time, end_time)
                    values ('${JOB_ID}', -1, 0, 1)`)).rejects.toThrow();
  });

  it("is job-scoped: one job's rows never leak into another job's read", async () => {
    await seedJob(JOB_ID);
    await seedJob(OTHER_JOB_ID);
    await insertRow(JOB_ID, 0, 0, 10);
    const other = await q(`select count(*)::int as n from public.analysis_timeline where job_id = '${OTHER_JOB_ID}'`);
    expect(other[0].n).toBe(0);
    const mine = await q(`select count(*)::int as n from public.analysis_timeline where job_id = '${JOB_ID}'`);
    expect(mine[0].n).toBe(1);
  });

  it("denies the anon role (RLS deny-by-default, service-role only)", async () => {
    await seedJob(JOB_ID);
    await insertRow(JOB_ID, 0, 0, 10);
    // The service role (bypassrls) sees the rows — the worker's path.
    // try/finally guarantees the connection role is always reset, so a
    // failure here can never poison later tests.
    try {
      await db!.exec(`set role service_role`);
      const asService = await q(`select count(*)::int as n from public.analysis_timeline where job_id = '${JOB_ID}'`);
      expect(asService[0].n).toBeGreaterThan(0);
    } finally {
      await db!.exec(`reset role`);
    }

    // anon: no table privileges + no policy → denied outright.
    await db!.exec(`set role anon`);
    try {
      await expect(q(`select count(*)::int as n from public.analysis_timeline where job_id = '${JOB_ID}'`))
        .rejects.toThrow();
      await expect(q(`insert into public.analysis_timeline
                      (job_id, window_index, start_time, end_time)
                      values ('${JOB_ID}', 99, 0, 1)`)).rejects.toThrow();
    } finally {
      await db!.exec(`reset role`);
    }
  });

  it("cascades with the job (job-level cleanup leaves no orphans)", async () => {
    await seedJob(OTHER_JOB_ID);
    await insertRow(OTHER_JOB_ID, 0, 0, 10);
    expect((await q(`select count(*)::int as n from public.analysis_timeline where job_id = '${OTHER_JOB_ID}'`))[0].n).toBe(1);
    await q(`delete from public.jobs where id = '${OTHER_JOB_ID}'`);
    expect((await q(`select count(*)::int as n from public.analysis_timeline where job_id = '${OTHER_JOB_ID}'`))[0].n).toBe(0);
  });

  it("is idempotent (re-applying is a no-op)", async () => {
    await seedJob(JOB_ID);
    await insertRow(JOB_ID, 0, 0, 10);
    const sql = readFileSync(join(MIGRATIONS_DIR, "20261128000000_task15_analysis_timeline.sql"), "utf-8");
    await db!.exec(sql);
    const rows = await q(`select count(*)::int as n from public.analysis_timeline where job_id = '${JOB_ID}'`);
    // existing rows survive the re-apply
    expect(rows[0].n).toBe(1);
  });
});
