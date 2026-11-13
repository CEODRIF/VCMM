/**
 * Phase 4 — analysis-stage queue tests against a REAL PostgreSQL engine.
 *
 * Verifies the stage-aware extensions of the queue RPCs:
 *   - a `ready` job is claimed into `transcribing` (the analysis path)
 *   - crashed jobs in transcribing/analyzing are recovered via expired leases
 *     (stage preserved)
 *   - transient analysis failures requeue to `ready` (no re-ingestion)
 *   - lease/progress/renew work in every active stage, owner-scoped
 *   - parked `analyzing` jobs (lock cleared) are never re-claimed
 *   - clips gained score/scoring columns (non-destructive)
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
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

let sessionSeq = 0;
let jobSeq = 0;

/**
 * Seeds a session + an upload job in the requested status, following the
 * LEGAL transition path (uploading → queued → validating → ready).
 * 'uploading' means the finished-upload state (queued); 'ready' the state
 * Phase 4 analysis consumes.
 */
async function seedJob(status: "uploading" | "ready", overrides: { createdAtOffsetSeconds?: number; maxAttempts?: number } = {}): Promise<string> {
  sessionSeq += 1;
  jobSeq += 1;
  const sessionId = `77777777-7777-4777-8777-${String(777777777000 + sessionSeq).padStart(12, "0")}`;
  const jobId = `88888888-8888-4888-8888-${String(888888888000 + jobSeq).padStart(12, "0")}`;
  const createdOffset = overrides.createdAtOffsetSeconds ?? 0;
  await db.query(
    `insert into public.processing_sessions (id, token_hash, expires_at) values ('${sessionId}', 'p4-${jobSeq}', now() + interval '1 hour')`
  );
  await db.query(
    `insert into public.jobs
        (id, session_id, source_type, source_path, requested_clips, status, max_attempts, created_at)
     values ('${jobId}', '${sessionId}', 'upload', 'sources/${jobId}/reserved', 1, 'uploading',
             ${overrides.maxAttempts ?? 3}, now() - (${createdOffset} * interval '1 second'))`
  );
  if (status === "ready") {
    await db.query(`update public.jobs set status = 'queued', upload_completed_at = now() where id = '${jobId}'`);
    await db.query(`update public.jobs set status = 'validating', locked_by = 'seed', locked_at = now(), lease_expires_at = now() + interval '5 minutes' where id = '${jobId}'`);
    await db.query(`update public.jobs set status = 'ready', locked_by = null, locked_at = null, lease_expires_at = null where id = '${jobId}'`);
  } else {
    await db.query(`update public.jobs set status = 'queued', upload_completed_at = now() where id = '${jobId}'`);
  }
  return jobId;
}

function cleanQueue() {
  return db.query("delete from public.jobs");
}

beforeAll(async () => {
  db = await PGlite.create();

  await db.exec(`
    create role anon noinherit nologin;
    create role service_role noinherit nologin bypassrls;
    grant usage on schema public to anon, service_role;
  `);

  const migrationsDir = join(process.cwd(), "supabase", "migrations");
  for (const file of [
    "20261113000000_initial_schema.sql",
    "20261114000000_phase2_job_settings_and_session_lookup.sql",
    "20261115000000_phase3_queue_and_ingestion.sql",
    "20261116000000_phase4_clip_analysis.sql",
  ]) {
    await db.exec(readFileSync(join(migrationsDir, file), "utf-8"));
  }

  await db.exec(`
    grant select, insert, update, delete on public.processing_sessions, public.jobs, public.clips to anon, service_role;
  `);
});

afterAll(async () => {
  await db?.close();
});

describe("Phase 4 clips schema", () => {
  it("adds score + scoring to clips without touching existing columns", async () => {
    const columns = (await q("select column_name from information_schema.columns where table_name = 'clips'")).map(
      (c) => c.column_name as string
    );
    for (const expected of ["score", "scoring", "title", "start_time", "end_time", "sort_order", "status"]) {
      expect(columns, expected).toContain(expected);
    }
  });

  it("enforces the 0-100 score bound", async () => {
    const sessionId = "99999999-9999-4999-8999-999999999001";
    const jobId = "99999999-9999-4999-8999-999999999002";
    await db.query(
      `insert into public.processing_sessions (id, token_hash, expires_at) values ('${sessionId}', 'p4-score', now() + interval '1 hour')`
    );
    await db.query(
      `insert into public.jobs (id, session_id, source_type, source_path, requested_clips, status)
       values ('${jobId}', '${sessionId}', 'upload', 'sources/${jobId}/reserved', 1, 'ready')`
    );
    await expectSqlError(
      `insert into public.clips (job_id, title, start_time, end_time, sort_order, score) values ('${jobId}', 't', 0, 90, 1, 101)`,
      /check/i
    );
    // A valid row succeeds with the explainable scoring payload.
    await db.query(
      `insert into public.clips (job_id, title, start_time, end_time, sort_order, score, scoring)
       values ('${jobId}', 't', 0, 90, 1, 87.5, '{"hook":{"value":0.9,"reason":"x"}}'::jsonb)`
    );
    const score = (await scalar(
      `select score from public.clips where job_id = '${jobId}'`
    )) as number;
    expect(Number(score)).toBe(87.5);
    // Pre-Phase-4 rows (score omitted) default to 0 — existing data is preserved.
    await db.query(`insert into public.clips (job_id, title, start_time, end_time, sort_order) values ('${jobId}', 'old', 100, 160, 2)`);
    const defaultScore = (await scalar(
      `select score from public.clips where job_id = '${jobId}' and title = 'old'`
    )) as number;
    expect(Number(defaultScore)).toBe(0);
  });
});

describe("stage-aware claiming (Phase 4)", () => {
  it("claims a ready job into the transcribing stage (analysis path)", async () => {
    await cleanQueue();
    const readyJob = await seedJob("ready");
    await db.query("set role service_role");
    const claimed = (await one(
      "select id::text as id, status, locked_by, attempt_count from public.claim_next_job('p4-w1', 300)"
    )) as Row;
    await db.query("set role postgres");
    expect(claimed?.id).toBe(readyJob);
    expect(claimed?.status).toBe("transcribing");
    expect(claimed?.locked_by).toBe("p4-w1");
    expect(claimed?.attempt_count).toBe(1);
  });

  it("still claims queued jobs into validating (ingestion path, Phase 3 regression)", async () => {
    await cleanQueue();
    const queuedJob = await seedJob("uploading");
    await db.query("set role service_role");
    const claimed = (await one("select id::text as id, status from public.claim_next_job('p4-w2', 300)")) as Row;
    await db.query("set role postgres");
    expect(claimed?.id).toBe(queuedJob);
    expect(claimed?.status).toBe("validating");
  });

  it("claims oldest first across mixed stages", async () => {
    await cleanQueue();
    const olderReady = await seedJob("ready", { createdAtOffsetSeconds: 30 });
    const newerQueued = await seedJob("uploading", { createdAtOffsetSeconds: 5 });
    await db.query("set role service_role");
    const claimed = (await one("select id::text as id from public.claim_next_job('p4-w3', 300)")) as Row;
    await db.query("set role postgres");
    expect(claimed?.id).toBe(olderReady);
    // the queued job is still claimable afterwards
    const second = (await one("select id::text as id, status from public.claim_next_job('p4-w3', 300)")) as Row;
    await db.query("set role postgres");
    expect(second?.id).toBe(newerQueued);
    expect(second?.status).toBe("validating");
  });

  it("recovers a crashed transcribing job via expired lease (stage preserved)", async () => {
    await cleanQueue();
    const jobId = await seedJob("ready");
    await db.query("set role service_role");
    const first = (await one("select id::text as id from public.claim_next_job('p4-crash', 30)")) as Row;
    expect(first?.id).toBe(jobId); // now transcribing, lease 30s
    // Worker "crashes": expire the lease manually.
    await db.query(`update public.jobs set lease_expires_at = now() - interval '1 minute' where id = '${jobId}'`);
    const recovered = (await one(
      "select id::text as id, status, locked_by, attempt_count from public.claim_next_job('p4-rescue', 300)"
    )) as Row;
    await db.query("set role postgres");
    expect(recovered?.id).toBe(jobId);
    expect(recovered?.status).toBe("transcribing"); // NOT re-routed to validating
    expect(recovered?.locked_by).toBe("p4-rescue");
    expect(recovered?.attempt_count).toBe(2);
  });

  it("recovers a crashed analyzing job via expired lease (stage preserved)", async () => {
    await cleanQueue();
    const jobId = await seedJob("ready");
    await db.query("set role service_role");
    await db.query(`select public.claim_next_job('p4-a', 30)`);
    // Move it to analyzing (legal) and expire the lease.
    await db.query(`update public.jobs set status = 'analyzing' where id = '${jobId}'`);
    await db.query(`update public.jobs set lease_expires_at = now() - interval '1 minute' where id = '${jobId}'`);
    const recovered = (await one(
      "select id::text as id, status, locked_by from public.claim_next_job('p4-b', 300)"
    )) as Row;
    await db.query("set role postgres");
    expect(recovered?.id).toBe(jobId);
    expect(recovered?.status).toBe("analyzing");
    expect(recovered?.locked_by).toBe("p4-b");
  });

  it("never claims a PARKED analyzing job (lock cleared, no lease)", async () => {
    await cleanQueue();
    const jobId = await seedJob("ready");
    await db.query("set role service_role");
    await db.query(`select public.claim_next_job('p4-p', 300)`);
    await db.query(`update public.jobs set status = 'analyzing' where id = '${jobId}'`);
    await db.query(`update public.jobs set locked_by = null, locked_at = null, lease_expires_at = null where id = '${jobId}'`);
    const claimed = (await one("select id::text as id from public.claim_next_job('p4-q', 300)")) as Row;
    await db.query("set role postgres");
    expect(claimed?.id ?? null).toBeNull();
    const status = await scalar(`select status from public.jobs where id = '${jobId}'`);
    expect(status).toBe("analyzing");
  });

  it("never re-claims an active transcribing job with an unexpired lease", async () => {
    await cleanQueue();
    await seedJob("ready");
    await db.query("set role service_role");
    const first = (await one("select id::text as id from public.claim_next_job('p4-active', 3600)")) as Row;
    expect(first).toBeTruthy();
    const second = (await one("select id::text as id from public.claim_next_job('p4-other', 300)")) as Row;
    await db.query("set role postgres");
    expect(second?.id ?? null).toBeNull();
  });
});

describe("analysis-stage failure semantics", () => {
  it("a transient transcribing failure requeues to READY (no re-ingestion)", async () => {
    await cleanQueue();
    const jobId = await seedJob("ready");
    await db.query("set role service_role");
    await db.query(`select public.claim_next_job('p4-f1', 300)`); // → transcribing
    const newStatus = (await scalar(
      `select public.fail_job('${jobId}'::uuid, 'p4-f1', 'transcription_failed', 'provider timeout', false)`
    )) as string;
    const row = (await one(`select status, locked_by, last_error from public.jobs where id = '${jobId}'`)) as Row;
    await db.query("set role postgres");
    expect(newStatus).toBe("ready");
    expect(row?.status).toBe("ready");
    expect(row?.locked_by ?? null).toBeNull();
    expect(String(row?.last_error ?? "")).toMatch(/^transcription_failed: /);
    // The re-claimed job enters analysis again, not ingestion.
    const claimed = (await one("select status from public.claim_next_job('p4-f2', 300)")) as Row;
    await db.query("set role postgres");
    expect(claimed?.status).toBe("transcribing");
  });

  it("a permanent transcribing failure fails the job immediately", async () => {
    await cleanQueue();
    const jobId = await seedJob("ready");
    await db.query("set role service_role");
    await db.query(`select public.claim_next_job('p4-f3', 300)`);
    const newStatus = (await scalar(
      `select public.fail_job('${jobId}'::uuid, 'p4-f3', 'no_audio', 'no audio track', true)`
    )) as string;
    await db.query("set role postgres");
    expect(newStatus).toBe("failed");
  });

  it("a transient failure at max attempts from analyzing terminates at failed", async () => {
    await cleanQueue();
    const jobId = await seedJob("ready", { maxAttempts: 2 });
    await db.query("set role service_role");
    await db.query(`select public.claim_next_job('p4-f4', 300)`); // attempt 1 → transcribing
    await db.query(`update public.jobs set status = 'analyzing' where id = '${jobId}'`);
    await db.query(`update public.jobs set lease_expires_at = now() - interval '1 minute' where id = '${jobId}'`);
    await db.query(`select public.claim_next_job('p4-f4b', 300)`); // attempt 2 (recovery) → analyzing
    const newStatus = (await scalar(
      `select public.fail_job('${jobId}'::uuid, 'p4-f4b', 'clips_persistence_failed', 'db error', false)`
    )) as string;
    await db.query("set role postgres");
    expect(newStatus).toBe("failed");
  });

  it("release_job from analyzing returns the job to ready", async () => {
    await cleanQueue();
    const jobId = await seedJob("ready");
    await db.query("set role service_role");
    await db.query(`select public.claim_next_job('p4-r', 300)`); // transcribing
    await db.query(`update public.jobs set status = 'analyzing' where id = '${jobId}'`);
    const released = (await scalar(`select public.release_job('${jobId}'::uuid, 'p4-r')`)) as boolean;
    await db.query("set role postgres");
    expect(released).toBe(true);
    const status = await scalar(`select status from public.jobs where id = '${jobId}'`);
    expect(status).toBe("ready");
  });

  it("release_job from validating still returns to queued (Phase 3 regression)", async () => {
    await cleanQueue();
    const jobId = await seedJob("uploading");
    await db.query("set role service_role");
    await db.query(`select public.claim_next_job('p4-r2', 300)`); // validating
    const released = (await scalar(`select public.release_job('${jobId}'::uuid, 'p4-r2')`)) as boolean;
    await db.query("set role postgres");
    expect(released).toBe(true);
    const status = await scalar(`select status from public.jobs where id = '${jobId}'`);
    expect(status).toBe("queued");
  });
});

describe("lease ownership across analysis stages", () => {
  it("renew + progress work in transcribing for the owner only", async () => {
    await cleanQueue();
    const jobId = await seedJob("ready");
    await db.query("set role service_role");
    await db.query(`select public.claim_next_job('p4-l', 300)`); // transcribing
    const strangerRenew = (await scalar(
      `select public.renew_job_lease('${jobId}'::uuid, 'p4-x', 300)`
    )) as boolean;
    expect(strangerRenew).toBe(false);
    const ownerRenew = (await scalar(
      `select public.renew_job_lease('${jobId}'::uuid, 'p4-l', 300)`
    )) as boolean;
    expect(ownerRenew).toBe(true);
    const strangerProgress = (await scalar(
      `select public.update_job_progress('${jobId}'::uuid, 'p4-x', 50)`
    )) as boolean;
    expect(strangerProgress).toBe(false);
    const ownerProgress = (await scalar(
      `select public.update_job_progress('${jobId}'::uuid, 'p4-l', 55)`
    )) as boolean;
    expect(ownerProgress).toBe(true);
    await db.query("set role postgres");
    expect(await scalar(`select progress from public.jobs where id = '${jobId}'`)).toBe(55);
  });

  it("fail_job from another worker is refused (not_owner)", async () => {
    await cleanQueue();
    const jobId = await seedJob("ready");
    await db.query("set role service_role");
    await db.query(`select public.claim_next_job('p4-own', 300)`);
    const result = (await scalar(
      `select public.fail_job('${jobId}'::uuid, 'p4-intruder', 'unknown', 'x', false)`
    )) as string;
    await db.query("set role postgres");
    expect(result).toBe("not_owner");
  });
});

describe("Phase 4 transition map", () => {
  const WITH_LOCK = "locked_by = 't', locked_at = now(), lease_expires_at = now() + interval '5 minutes'";
  const NO_LOCK = "locked_by = null, locked_at = null, lease_expires_at = null";

  it("allows ready → transcribing → analyzing → parked, and transcribing → ready (retry)", async () => {
    await cleanQueue();
    const jobId = await seedJob("ready");
    await db.query(`update public.jobs set status = 'transcribing', ${WITH_LOCK} where id = '${jobId}'`);
    await db.query(`update public.jobs set status = 'ready', ${NO_LOCK} where id = '${jobId}'`); // retry path
    await db.query(`update public.jobs set status = 'transcribing', ${WITH_LOCK} where id = '${jobId}'`);
    await db.query(`update public.jobs set status = 'analyzing' where id = '${jobId}'`); // lock kept
    // Parked: analysis finished, lock cleared — legal.
    await db.query(`update public.jobs set ${NO_LOCK} where id = '${jobId}'`);
    expect(await scalar(`select status from public.jobs where id = '${jobId}'`)).toBe("analyzing");
  });

  it("still rejects illegal transitions (analyzing → queued, ready → queued)", async () => {
    await cleanQueue();
    const jobId = await seedJob("ready");
    await expectSqlError(`update public.jobs set status = 'queued' where id = '${jobId}'`, /illegal job status transition/i);
    await db.query(`update public.jobs set status = 'transcribing', ${WITH_LOCK} where id = '${jobId}'`);
    await db.query(`update public.jobs set status = 'analyzing' where id = '${jobId}'`);
    await expectSqlError(`update public.jobs set status = 'queued' where id = '${jobId}'`, /illegal job status transition analyzing -> queued/i);
  });

  it("denies the extended RPCs to anon (privileges unchanged)", async () => {
    await db.query("set role anon");
    await expectSqlError("select public.claim_next_job('anon-p4', 120)", /permission denied/i);
    await db.query("set role postgres");
  });
});
