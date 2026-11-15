/**
 * TASK 015 P3 — generation modes + explicit output FPS schema test against a
 * REAL PostgreSQL engine (PGlite WASM).
 *
 * Verifies the repository-only migration
 * `20261129000000_task15_generation_modes.sql`:
 *   - jobs.generation_mode (default 'highlights', check in both modes);
 *   - jobs.output_fps (NULL legacy | 30 | 60 only);
 *   - jobs.requested_clips nullable + the mode/count consistency guard
 *     (full_coverage => NULL — a user count must NEVER truncate coverage;
 *     highlights => 1..10 upper bound);
 *   - clips.mode (which generation mode produced the row);
 *   - clips.duration bounds generalized to 5..3600 (full-coverage clips
 *     may be shorter; no legacy row shape breaks);
 *   - re-applying the migration is a no-op (idempotent).
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
  "20261129000000_task15_generation_modes.sql",
];

const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";

type Row = Record<string, unknown>;

async function q(sql: string): Promise<Row[]> {
  const result = await db!.query(sql);
  return result.rows as Row[];
}

beforeAll(async () => {
  db = await PGlite.create();
  // The role surface the migrations reference (Supabase parity).
  await db.exec(`
    create role anon noinherit nologin;
    create role service_role noinherit nologin bypassrls;
    grant usage on schema public to anon, service_role;
  `);
  for (const f of MIGRATIONS) {
    await db.exec(readFileSync(join(MIGRATIONS_DIR, f), "utf-8"));
  }
});

afterAll(async () => {
  await db?.close();
});

beforeEach(async () => {
  await db!.exec(`delete from public.clips; delete from public.analysis_timeline; delete from public.jobs;`);
  await q(`insert into public.processing_sessions (id, token_hash, expires_at)
           values ('${SESSION_ID}', 't', now() + interval '1 hour') on conflict do nothing`);
});

/** Inserts a job row with the P3 columns set explicitly. */
async function seedJob(
  jobId: string,
  generationMode: string,
  outputFps: number | null,
  requestedClips: number | null
): Promise<void> {
  await q(`insert into public.jobs
           (id, session_id, source_type, source_path, requested_clips,
            clip_duration_seconds, status, generation_mode, output_fps)
           values ('${jobId}', '${SESSION_ID}', 'upload',
                   'sources/${jobId}/original/generated.mp4',
                   ${requestedClips === null ? "null" : requestedClips},
                   60, 'queued', '${generationMode}',
                   ${outputFps === null ? "null" : outputFps})`);
}

/** Inserts a clip with an explicit window (duration is DB-generated). */
async function seedClip(jobId: string, start: number, end: number, mode?: string): Promise<void> {
  await q(`insert into public.clips (job_id, title, start_time, end_time${mode ? ", mode" : ""})
           values ('${jobId}', 'clip', ${start}, ${end}${mode ? `, '${mode}'` : ""})`);
}

describe("TASK 015 P3 migration: jobs generation modes + output fps", () => {
  it("keeps the legacy row shape working (defaults applied)", async () => {
    // A pre-P3 insert: no generation_mode / output_fps columns mentioned.
    await q(`insert into public.jobs
             (id, session_id, source_type, source_path, requested_clips,
              clip_duration_seconds, status)
             values ('${JOB_ID}', '${SESSION_ID}', 'upload',
                     'sources/${JOB_ID}/original/generated.mp4', 3, 60, 'queued')`);
    const row = (await q(`select generation_mode, output_fps, requested_clips
                          from public.jobs where id = '${JOB_ID}'`))[0]!;
    expect(row.generation_mode).toBe("highlights");
    expect(row.output_fps).toBeNull();
    expect(row.requested_clips).toBe(3);
  });

  it("accepts the explicit combinations and rejects invalid ones", async () => {
    // highlights + upper bound (1..10) + each fps choice / legacy null.
    await seedJob(JOB_ID, "highlights", null, 3);
    const j2 = "44444444-4444-4444-8444-444444444444";
    const j3 = "55555555-5555-4555-8555-555555555555";
    const j4 = "66666666-6666-4666-8666-666666666666";
    const j5 = "77777777-7777-4777-8777-777777777777";
    await seedJob(j2, "highlights", 30, 1);
    await seedJob(j3, "highlights", 60, 10);
    await seedJob(j4, "full_coverage", null, null);
    await seedJob(j5, "full_coverage", 60, null);

    // invalid fps values
    await expect(seedJob("88888888-8888-4888-8888-888888888888", "highlights", 24, 3))
      .rejects.toThrow();
    await expect(seedJob("88888888-8888-4888-8888-888888888888", "highlights", 120, 3))
      .rejects.toThrow();
    // invalid generation mode
    await expect(seedJob("88888888-8888-4888-8888-888888888888", "bogus", null, 3))
      .rejects.toThrow();
    // full_coverage with a user count: NEVER allowed (it would truncate coverage)
    await expect(seedJob("88888888-8888-4888-8888-888888888888", "full_coverage", null, 5))
      .rejects.toThrow();
    // highlights without a bound / out of the 1..10 band
    await expect(seedJob("99999999-9999-4999-8999-999999999999", "highlights", null, null))
      .rejects.toThrow();
    await expect(seedJob("99999999-9999-4999-8999-999999999999", "highlights", null, 15))
      .rejects.toThrow();
    await expect(seedJob("99999999-9999-4999-8999-999999999999", "highlights", null, 0))
      .rejects.toThrow();
  });

  it("persists the chosen fps for both modes", async () => {
    await seedJob(JOB_ID, "full_coverage", 60, null);
    const row = (await q(`select output_fps, generation_mode from public.jobs where id = '${JOB_ID}'`))[0]!;
    expect(row.output_fps).toBe(60);
    expect(row.generation_mode).toBe("full_coverage");
  });
});

describe("TASK 015 P3 migration: clips mode + duration bounds", () => {
  it("accepts a full-coverage short clip and a long window (5..3600 bounds)", async () => {
    await seedJob(JOB_ID, "full_coverage", 60, null);
    // remainder-floor clip (>= 5s) with the mode tag.
    await seedClip(JOB_ID, 0, 15, "full_coverage");
    // a full window + merged remainder (<= 3600s).
    await seedClip(JOB_ID, 15, 195, "full_coverage");
    const rows = await q(`select duration, mode from public.clips where job_id = '${JOB_ID}' order by start_time`);
    expect(rows).toHaveLength(2);
    // `duration` is a generated numeric column — PGlite returns numerics as
    // strings; compare the numeric value.
    expect(Number(rows[0]!.duration)).toBe(15);
    expect(Number(rows[1]!.duration)).toBe(180);
    expect(rows[0]!.mode).toBe("full_coverage");
  });

  it("defaults legacy clips to the highlights mode", async () => {
    await seedJob(JOB_ID, "highlights", null, 3);
    await seedClip(JOB_ID, 0, 90);
    const row = (await q(`select mode from public.clips where job_id = '${JOB_ID}'`))[0]!;
    expect(row.mode).toBe("highlights");
  });

  it("rejects out-of-bounds durations and unknown modes", async () => {
    await seedJob(JOB_ID, "full_coverage", null, null);
    // below the 5s floor (useless reel)
    await expect(seedClip(JOB_ID, 0, 4)).rejects.toThrow();
    // above the 3600s ceiling
    await expect(seedClip(JOB_ID, 0, 3601)).rejects.toThrow();
    // unknown mode
    await expect(seedClip(JOB_ID, 0, 60, "bogus")).rejects.toThrow();
    // the 3600s boundary itself is allowed
    await seedClip(JOB_ID, 0, 3600);
    const row = (await q(`select duration from public.clips where job_id = '${JOB_ID}'`))[0]!;
    expect(Number(row.duration)).toBe(3600);
  });
});

describe("TASK 015 P3 migration: idempotency", () => {
  it("re-applying 20261129 is a no-op and existing rows survive", async () => {
    await seedJob(JOB_ID, "full_coverage", 60, null);
    await seedClip(JOB_ID, 0, 15, "full_coverage");
    const sql = readFileSync(join(MIGRATIONS_DIR, "20261129000000_task15_generation_modes.sql"), "utf-8");
    await db!.exec(sql);
    const job = (await q(`select generation_mode, output_fps, requested_clips
                           from public.jobs where id = '${JOB_ID}'`))[0]!;
    expect(job.generation_mode).toBe("full_coverage");
    expect(job.output_fps).toBe(60);
    expect(job.requested_clips).toBeNull();
    const clip = (await q(`select duration, mode from public.clips where job_id = '${JOB_ID}'`))[0]!;
    expect(Number(clip.duration)).toBe(15);
    expect(clip.mode).toBe("full_coverage");
  });
});
