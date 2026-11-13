/**
 * Phase 7b (TASK 007) — stream-intelligence INTEGRATION queue tests against a
 * REAL PostgreSQL engine (PGlite WASM).
 *
 * Verifies the additive migration `20261125000000`:
 *   - `analysis_resumes` column (default 0, >= 0) on jobs — a dedicated
 *     incomplete-analysis resume counter (the render_attempts precedent);
 *   - `requeue_incomplete_analysis(job_id, worker_id, max_resumes)`:
 *       * 'not_owner' when the caller does not own an active analysis job;
 *       * 'ready' + analysis_resumes + 1 + lock cleared, attempt_count UNCHANGED,
 *         for an owned transcribing OR analyzing job (the "incomplete,
 *         continue later" transition — distinct from park / release / fail);
 *       * 'resume_exhausted' (NO state write) when the resume cap is reached;
 *       * never requeues a 'ready' job or a PARKED (lock-cleared) job;
 *   - `claim_next_job` resume branch:
 *       * a 'ready' job with analysis_resumes > 0 is claimable EVEN when
 *         attempt_count >= max_attempts (a job making progress is not starved
 *         by the failure budget);
 *       * a resume claim does NOT increment attempt_count;
 *       * it moves the job to 'transcribing' with the worker's lease;
 *   - BACKWARD COMPATIBILITY (Phases 3-6 unchanged):
 *       * a 'ready' job with analysis_resumes = 0 and an exhausted attempt
 *         budget is NOT claimed (the failure safety valve still holds);
 *       * a normal (non-resume) claim still increments attempt_count;
 *       * a PARKED analyzing job (lock cleared) is still claimed to rendering
 *         by the render budget, not confused with a resume;
 *       * expired-lease crash recovery still works;
 *   - re-applying the migration is a no-op; anon is still denied the new RPC.
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
  return ((await db.query(sql)).rows as Row[])[0];
}
async function scalar(sql: string): Promise<unknown> {
  const row = await one(sql);
  return row ? Object.values(row)[0] : null;
}

let sessionSeq = 0;
let jobSeq = 0;

/**
 * Seeds a session + upload job and walks the LEGAL transition path to the
 * requested state, optionally setting attempt_count / analysis_resumes /
 * lock ownership. States:
 *   "ready"          — analysis entry point (lock cleared);
 *   "transcribing"   — active, locked by `lockBy` (default 'seed');
 *   "analyzing"      — active, locked by `lockBy` (default 'seed');
 *   "parked"         — analyzing with the lock CLEARED (Phase 4 end state).
 */
async function seedJob(
  status: "ready" | "transcribing" | "analyzing" | "parked",
  options: {
    attemptCount?: number;
    analysisResumes?: number;
    maxAttempts?: number;
    lockBy?: string;
    leaseSeconds?: number;
    leaseExpired?: boolean;
    createdAtOffsetSeconds?: number;
  } = {}
): Promise<string> {
  sessionSeq += 1;
  jobSeq += 1;
  const sessionId = `99999999-9999-4999-8999-${String(999999999000 + sessionSeq).padStart(12, "0")}`;
  const jobId = `aaaaaaaa-aaaa-4aaa-8aaa-${String(111111111000 + jobSeq).padStart(12, "0")}`;
  const maxAttempts = options.maxAttempts ?? 3;
  const createdOffset = options.createdAtOffsetSeconds ?? 0;
  const lockBy = options.lockBy ?? "seed";
  const lease = (expired: boolean) =>
    `lease_expires_at = ${expired ? "now() - interval '1 minute'" : `now() + interval '${options.leaseSeconds ?? 5} minutes'`}`;

  await db.query(
    `insert into public.processing_sessions (id, token_hash, expires_at) values ('${sessionId}', 'p7b-${jobSeq}', now() + interval '1 hour')`
  );
  await db.query(
    `insert into public.jobs
        (id, session_id, source_type, source_path, requested_clips, status, max_attempts, created_at)
     values ('${jobId}', '${sessionId}', 'upload', 'sources/${jobId}/original/v.mp4', 1, 'uploading',
             ${maxAttempts}, now() - (${createdOffset} * interval '1 second'))`
  );
  // uploading → queued → validating (locked) → ready (cleared)
  await db.query(`update public.jobs set status = 'queued', upload_completed_at = now() where id = '${jobId}'`);
  await db.query(`update public.jobs set status = 'validating', locked_by = '${lockBy}', locked_at = now(), ${lease(false)} where id = '${jobId}'`);
  await db.query(`update public.jobs set status = 'ready', locked_by = null, locked_at = null, lease_expires_at = null where id = '${jobId}'`);
  if (status === "transcribing" || status === "analyzing" || status === "parked") {
    // ready → transcribing (locked)
    await db.query(`update public.jobs set status = 'transcribing', locked_by = '${lockBy}', locked_at = now(), ${lease(options.leaseExpired ?? false)} where id = '${jobId}'`);
  }
  if (status === "analyzing" || status === "parked") {
    // transcribing → analyzing (locked)
    await db.query(`update public.jobs set status = 'analyzing', locked_by = '${lockBy}', locked_at = now(), ${lease(options.leaseExpired ?? false)} where id = '${jobId}'`);
  }
  if (status === "parked") {
    // clear the lock = Phase 4 "done, awaiting rendering"
    await db.query(`update public.jobs set locked_by = null, locked_at = null, lease_expires_at = null, progress = 100 where id = '${jobId}'`);
  }
  if (options.attemptCount != null) {
    await db.query(`update public.jobs set attempt_count = ${options.attemptCount} where id = '${jobId}'`);
  }
  if (options.analysisResumes != null) {
    await db.query(`update public.jobs set analysis_resumes = ${options.analysisResumes} where id = '${jobId}'`);
  }
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
  "20261125000000_phase7b_intel_integration.sql",
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
  await db.query("delete from public.clips");
  await db.query("delete from public.processing_checkpoints");
  await db.query("delete from public.candidate_events");
  await db.query("delete from public.jobs");
});

describe("Phase 7b migration: analysis_resumes column", () => {
  it("exists with default 0 and a >= 0 check; existing rows are untouched", async () => {
    const cols = await q(
      `select column_name, column_default, is_nullable from information_schema.columns
       where table_schema = 'public' and table_name = 'jobs' and column_name = 'analysis_resumes'`
    );
    expect(cols).toHaveLength(1);
    expect(cols[0]).toMatchObject({ column_default: "0", is_nullable: "NO" });

    const jobId = await seedJob("ready");
    expect(await scalar(`select analysis_resumes from public.jobs where id = '${jobId}'`)).toBe(0);

    // The check rejects negative values.
    await expect(
      db.query(`update public.jobs set analysis_resumes = -1 where id = '${jobId}'`)
    ).rejects.toThrow();
  });

  it("re-applying the migration is a no-op", async () => {
    // Re-applying must not error (exec resolves; it returns per-statement
    // result sets, so we assert on resolution, not the return shape).
    await db.exec(
      readFileSync(join(MIGRATIONS_DIR, "20261125000000_phase7b_intel_integration.sql"), "utf-8")
    );
  });
});

describe("Phase 7b: requeue_incomplete_analysis", () => {
  it("returns not_owner when the caller does not own the job", async () => {
    const jobId = await seedJob("transcribing", { lockBy: "someone-else" });
    const out = await scalar(`select public.requeue_incomplete_analysis('${jobId}', 'me', 100)`);
    expect(out).toBe("not_owner");
    // No state change.
    const row = await one(`select status, analysis_resumes, locked_by from public.jobs where id = '${jobId}'`);
    expect(row?.status).toBe("transcribing");
    expect(row?.analysis_resumes).toBe(0);
    expect(row?.locked_by).toBe("someone-else");
  });

  it("requeues an owned TRANSCRIBING job to ready: resumes +1, attempt_count unchanged, lock cleared", async () => {
    const jobId = await seedJob("transcribing", { lockBy: "w7", attemptCount: 2 });
    const out = await scalar(`select public.requeue_incomplete_analysis('${jobId}', 'w7', 100)`);
    expect(out).toBe("ready");
    const row = await one(
      `select status, analysis_resumes, attempt_count, locked_by, lease_expires_at, last_error
       from public.jobs where id = '${jobId}'`
    );
    expect(row?.status).toBe("ready");
    expect(row?.analysis_resumes).toBe(1);
    expect(row?.attempt_count).toBe(2); // UNCHANGED — progress, not a failure
    expect(row?.locked_by).toBeNull();
    expect(row?.lease_expires_at).toBeNull();
  });

  it("requeues an owned ANALYZING job to ready as well", async () => {
    const jobId = await seedJob("analyzing", { lockBy: "w7" });
    const out = await scalar(`select public.requeue_incomplete_analysis('${jobId}', 'w7', 100)`);
    expect(out).toBe("ready");
    expect(await scalar(`select analysis_resumes from public.jobs where id = '${jobId}'`)).toBe(1);
  });

  it("never requeues a 'ready' job (only active transcribing/analyzing)", async () => {
    const jobId = await seedJob("ready", { analysisResumes: 1 });
    const out = await scalar(`select public.requeue_incomplete_analysis('${jobId}', 'w7', 100)`);
    expect(out).toBe("not_owner");
  });

  it("never requeues a PARKED (lock-cleared) analyzing job — that is the render path", async () => {
    const jobId = await seedJob("parked");
    const out = await scalar(`select public.requeue_incomplete_analysis('${jobId}', 'w7', 100)`);
    expect(out).toBe("not_owner");
    expect(await scalar(`select analysis_resumes from public.jobs where id = '${jobId}'`)).toBe(0);
  });

  it("returns resume_exhausted (no state write) when the cap is reached", async () => {
    const jobId = await seedJob("transcribing", { lockBy: "w7", analysisResumes: 99, attemptCount: 2 });
    // 99 + 1 >= 100 → exhausted.
    const out = await scalar(`select public.requeue_incomplete_analysis('${jobId}', 'w7', 100)`);
    expect(out).toBe("resume_exhausted");
    const row = await one(`select status, analysis_resumes, locked_by from public.jobs where id = '${jobId}'`);
    expect(row?.status).toBe("transcribing"); // unchanged
    expect(row?.analysis_resumes).toBe(99); // NOT incremented
    expect(row?.locked_by).toBe("w7"); // still owned
  });

  it("repeated resumes increment analysis_resumes each time (attempt_count stable)", async () => {
    const jobId = await seedJob("transcribing", { lockBy: "w7", attemptCount: 2 });
    for (let i = 1; i <= 3; i += 1) {
      // walk back to an active stage each time (ready → transcribing)
      await db.query(
        `update public.jobs set status = 'transcribing', locked_by = 'w7', locked_at = now(),
         lease_expires_at = now() + interval '5 minutes' where id = '${jobId}'`
      );
      const out = await scalar(`select public.requeue_incomplete_analysis('${jobId}', 'w7', 100000)`);
      expect(out).toBe("ready");
      expect(await scalar(`select analysis_resumes from public.jobs where id = '${jobId}'`)).toBe(i);
    }
    expect(await scalar(`select attempt_count from public.jobs where id = '${jobId}'`)).toBe(2);
  });
});

describe("Phase 7b: claim_next_job resume branch", () => {
  it("claims a 'ready' resume job (analysis_resumes > 0) even when the attempt budget is exhausted", async () => {
    // attempt_count = max_attempts (3) would normally block a ready claim, but
    // analysis_resumes > 0 makes it a mid-resume job → still claimable.
    const jobId = await seedJob("ready", { attemptCount: 3, analysisResumes: 1 });
    const claimed = await one(`select * from public.claim_next_job('w7', 300)`);
    expect(claimed?.id).toBe(jobId);
    expect(claimed?.status).toBe("transcribing");
    expect(claimed?.locked_by).toBe("w7");
    // The resume claim did NOT increment attempt_count.
    expect(claimed?.attempt_count).toBe(3);
    expect(await scalar(`select attempt_count from public.jobs where id = '${jobId}'`)).toBe(3);
  });

  it("does NOT claim a 'ready' job with analysis_resumes = 0 and an exhausted attempt budget (backward compat)", async () => {
    const jobId = await seedJob("ready", { attemptCount: 3 }); // no resumes, budget exhausted
    expect(await scalar(`select * from public.claim_next_job('w7', 300)`)).toBeNull();
    expect(await scalar(`select status from public.jobs where id = '${jobId}'`)).toBe("ready");
  });

  it("a normal (non-resume) claim still increments attempt_count (backward compat)", async () => {
    const jobId = await seedJob("ready"); // analysis_resumes = 0, attempt budget available
    const claimed = await one(`select * from public.claim_next_job('w7', 300)`);
    expect(claimed?.id).toBe(jobId);
    expect(claimed?.status).toBe("transcribing");
    expect(claimed?.attempt_count).toBe(1); // incremented from 0
  });

  it("a parked analyzing job (lock cleared) is still claimed to rendering by the render budget, not as a resume", async () => {
    const jobId = await seedJob("parked", { analysisResumes: 0 });
    // seed a clip so the render path is meaningful (claim does not need it, but
    // the parked→rendering transition is the Phase 5 behavior under test).
    const clipId = `bbbbbbbb-bbbb-4bbb-8bbb-${String(222222222000 + jobSeq).padStart(12, "0")}`;
    await db.query(
      `insert into public.clips (id, job_id, title, start_time, end_time, sort_order, status, storage_path)
       values ('${clipId}', '${jobId}', 'clip', 0, 90, 0, 'pending', null)`
    );
    const claimed = await one(`select * from public.claim_next_job('w7', 300)`);
    expect(claimed?.id).toBe(jobId);
    expect(claimed?.status).toBe("rendering"); // the render path, not transcribing
  });

  it("still recovers an expired-lease transcribing job (crash recovery, stage kept)", async () => {
    const jobId = await seedJob("transcribing", { lockBy: "crashed", leaseExpired: true });
    const claimed = await one(`select * from public.claim_next_job('w7', 300)`);
    expect(claimed?.id).toBe(jobId);
    expect(claimed?.status).toBe("transcribing");
    expect(claimed?.locked_by).toBe("w7");
  });

  it("the full resume cycle: ready(resume) → claim(transcribing, no attempt burn) → requeue → ready", async () => {
    const jobId = await seedJob("ready", { attemptCount: 3, analysisResumes: 2 });
    // Claim 1 (resume): transcribing, attempt_count stays 3.
    const c1 = await one(`select * from public.claim_next_job('w7', 300)`);
    expect(c1?.id).toBe(jobId);
    expect(c1?.status).toBe("transcribing");
    expect(c1?.attempt_count).toBe(3);
    // Incomplete → requeue (analysis_resumes 2 → 3).
    const out = await scalar(`select public.requeue_incomplete_analysis('${jobId}', 'w7', 100000)`);
    expect(out).toBe("ready");
    expect(await scalar(`select analysis_resumes from public.jobs where id = '${jobId}'`)).toBe(3);
    // Claim 2 (resume again): still claimable despite attempt_count = max, no burn.
    const c2 = await one(`select * from public.claim_next_job('w7', 300)`);
    expect(c2?.id).toBe(jobId);
    expect(c2?.status).toBe("transcribing");
    expect(c2?.attempt_count).toBe(3);
  });
});

describe("Phase 7b: privileges", () => {
  it("grants the new RPC to service_role and revokes it from public/anon", async () => {
    const grants = await q(
      `select grantee from has_function_privilege('anon', 'public.requeue_incomplete_analysis(uuid, text, integer)', 'EXECUTE') as grantee`
    );
    expect(grants[0]?.grantee).toBe(false);
    const svc = await q(
      `select grantee from has_function_privilege('service_role', 'public.requeue_incomplete_analysis(uuid, text, integer)', 'EXECUTE') as grantee`
    );
    expect(svc[0]?.grantee).toBe(true);
  });
});
