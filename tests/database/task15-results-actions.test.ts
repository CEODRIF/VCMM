/**
 * TASK 015 P4 — results dashboard actions schema test (PGlite WASM).
 *
 * Verifies the repository-only migration
 * `20261130000000_task15_results_actions.sql`:
 *   - clips media metadata columns (width/height/output_fps/file_size_bytes)
 *     with positivity guards and NULL-tolerance (measured, never fabricated);
 *   - clips.status now accepts 'deleting' (the permanent-deletion state)
 *     while still rejecting unknown statuses;
 *   - jobs.cleanup_error (partial-failure tracking for user cleanups);
 *   - compatibility: P3 rows (generation modes) and Phase 6 rows
 *     (purged_at/expires_at) coexist; re-apply is idempotent.
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
  "20261130000000_task15_results_actions.sql",
];

const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const CLIP_ID = "33333333-3333-4333-8333-333333333333";

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
});

afterAll(async () => {
  await db?.close();
});

beforeEach(async () => {
  await db!.exec(`delete from public.clips; delete from public.analysis_timeline; delete from public.jobs;`);
  await q(`insert into public.processing_sessions (id, token_hash, expires_at)
           values ('${SESSION_ID}', 't', now() + interval '1 hour') on conflict do nothing`);
  await q(`insert into public.jobs
           (id, session_id, source_type, source_path, requested_clips,
            clip_duration_seconds, status, generation_mode, output_fps,
            progress, completed_at)
           values ('${JOB_ID}', '${SESSION_ID}', 'upload',
                   'sources/${JOB_ID}/original/generated.mp4', 3, 60,
                   'completed', 'highlights', 30, 100, now())`);
});

describe("TASK 015 P4 migration: clip media metadata", () => {
  it("accepts measured metadata (NULL-tolerant, positive-gated)", async () => {
    await q(`insert into public.clips
             (id, job_id, title, start_time, end_time, status, storage_path,
              width, height, output_fps, file_size_bytes)
             values ('${CLIP_ID}', '${JOB_ID}', 'clip', 32, 92, 'ready',
                     'clips/${JOB_ID}/${CLIP_ID}.mp4', 1080, 1920, 59.94, 2500000)`);
    const row = (await q(`select width, height, output_fps, file_size_bytes
                           from public.clips where id = '${CLIP_ID}'`))[0]!;
    expect(row.width).toBe(1080);
    expect(row.height).toBe(1920);
    expect(Number(row.output_fps)).toBeCloseTo(59.94);
    expect(Number(row.file_size_bytes)).toBe(2500000);
  });

  it("allows NULL metadata (unrendered rows stay valid)", async () => {
    await q(`insert into public.clips
             (id, job_id, title, start_time, end_time, status)
             values ('${CLIP_ID}', '${JOB_ID}', 'clip', 32, 92, 'pending')`);
    const row = (await q(`select width, height, output_fps, file_size_bytes
                           from public.clips where id = '${CLIP_ID}'`))[0]!;
    expect(row.width).toBeNull();
    expect(row.output_fps).toBeNull();
  });

  it("rejects non-positive metadata values", async () => {
    await expect(q(`insert into public.clips
             (id, job_id, title, start_time, end_time, width)
             values ('${CLIP_ID}', '${JOB_ID}', 'c', 32, 92, 0)`)).rejects.toThrow();
    await expect(q(`insert into public.clips
             (id, job_id, title, start_time, end_time, output_fps)
             values ('${CLIP_ID}', '${JOB_ID}', 'c', 32, 92, -30)`)).rejects.toThrow();
    await expect(q(`insert into public.clips
             (id, job_id, title, start_time, end_time, file_size_bytes)
             values ('${CLIP_ID}', '${JOB_ID}', 'c', 32, 92, -1)`)).rejects.toThrow();
  });
});

describe("TASK 015 P4 migration: the 'deleting' clip state", () => {
  it("accepts 'deleting' and still rejects unknown statuses", async () => {
    await q(`insert into public.clips
             (id, job_id, title, start_time, end_time, status)
             values ('${CLIP_ID}', '${JOB_ID}', 'c', 32, 92, 'deleting')`);
    const row = (await q(`select status from public.clips where id = '${CLIP_ID}'`))[0]!;
    expect(row.status).toBe("deleting");

    await expect(q(`update public.clips set status = 'bogus' where id = '${CLIP_ID}'`))
      .rejects.toThrow();
  });

  it("the deletion transition is atomic from the current state only", async () => {
    await q(`insert into public.clips
             (id, job_id, title, start_time, end_time, status)
             values ('${CLIP_ID}', '${JOB_ID}', 'c', 32, 92, 'processing')`);
    // ready/failed → deleting is the protocol; a processing clip must NOT
    // match the guarded update (concurrent render protection, DB-level).
    const updated = await q(
      `update public.clips set status = 'deleting'
       where id = '${CLIP_ID}' and status in ('ready','failed')
       returning id`
    );
    expect(updated).toHaveLength(0);
    const row = (await q(`select status from public.clips where id = '${CLIP_ID}'`))[0]!;
    expect(row.status).toBe("processing");
  });
});

describe("TASK 015 P4 migration: job cleanup tracking", () => {
  it("cleanup_error defaults to NULL and stores partial-failure text", async () => {
    const initial = (await q(`select cleanup_error from public.jobs where id = '${JOB_ID}'`))[0]!;
    expect(initial.cleanup_error).toBeNull();
    await q(`update public.jobs set cleanup_error = 'clip x: storage unavailable' where id = '${JOB_ID}'`);
    const row = (await q(`select cleanup_error from public.jobs where id = '${JOB_ID}'`))[0]!;
    expect(row.cleanup_error).toContain("storage unavailable");
  });

  it("coexists with the Phase 6 purge marker + P3 generation modes", async () => {
    await q(`update public.jobs set purged_at = now(), cleanup_error = null where id = '${JOB_ID}'`);
    const row = (await q(
      `select status, generation_mode, output_fps, requested_clips, purged_at, cleanup_error
       from public.jobs where id = '${JOB_ID}'`
    ))[0]!;
    expect(row.status).toBe("completed");
    expect(row.generation_mode).toBe("highlights");
    expect(row.output_fps).toBe(30);
    expect(row.requested_clips).toBe(3);
    expect(row.purged_at).not.toBeNull();
    expect(row.cleanup_error).toBeNull();
  });
});

describe("TASK 015 P4 migration: idempotency", () => {
  it("re-applying 20261130 is a no-op and rows survive", async () => {
    await q(`insert into public.clips
             (id, job_id, title, start_time, end_time, status, width, height, output_fps)
             values ('${CLIP_ID}', '${JOB_ID}', 'c', 32, 92, 'deleting', 1080, 1920, 60)`);
    const sql = readFileSync(join(MIGRATIONS_DIR, "20261130000000_task15_results_actions.sql"), "utf-8");
    await db!.exec(sql);
    const clip = (await q(`select status, width from public.clips where id = '${CLIP_ID}'`))[0]!;
    expect(clip.status).toBe("deleting");
    expect(clip.width).toBe(1080);
    const job = (await q(`select cleanup_error from public.jobs where id = '${JOB_ID}'`))[0]!;
    expect(job.cleanup_error).toBeNull();
  });
});
