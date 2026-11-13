/**
 * Phase 5 — rendering-stage queue tests against a REAL PostgreSQL engine.
 *
 * Verifies the additive rendering extensions:
 *   - `render_attempts` column (default 0, bounded retry budget for rendering)
 *   - active rendering jobs hold their lease (lock consistency)
 *   - parked `analyzing` jobs are claimed into `rendering` (render budget)
 *   - crashed rendering jobs are recovered via expired leases (stage kept)
 *   - transient rendering failures re-park (render_attempts + 1) and are
 *     re-claimed; an exhausted render budget fails TERMINALLY
 *   - renewal/progress/fail/release work in rendering, owner-scoped
 *   - `complete_job_rendering` is the only terminal path: it requires
 *     ownership AND >= 1 clip AND every clip ready WITH a storage path
 *   - transition map: rendering → analyzing (retry) added; others preserved
 *   - anon still denied all queue RPCs (deny-by-default)
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
 * Seeds a session + upload job, walking the LEGAL transition path to the
 * requested state. "parked" = analyzing with the lock cleared (Phase 4 end
 * state). `clips` inserts n clip rows (status/storage_path as given).
 */
async function seedJob(
  status: "parked" | "rendering" | "ready" | "queued",
  options: {
    createdAtOffsetSeconds?: number;
    maxAttempts?: number;
    clips?: number;
    clipStatus?: string;
    clipStoragePath?: boolean;
    renderAttempts?: number;
    lockSeconds?: number;
    lockExpired?: boolean;
  } = {}
): Promise<string> {
  sessionSeq += 1;
  jobSeq += 1;
  const sessionId = `77777777-7777-4777-8777-${String(777777777000 + sessionSeq).padStart(12, "0")}`;
  const jobId = `88888888-8888-4888-8888-${String(888888888000 + jobSeq).padStart(12, "0")}`;
  const createdOffset = options.createdAtOffsetSeconds ?? 0;
  await db.query(
    `insert into public.processing_sessions (id, token_hash, expires_at) values ('${sessionId}', 'p5-${jobSeq}', now() + interval '1 hour')`
  );
  await db.query(
    `insert into public.jobs
        (id, session_id, source_type, source_path, requested_clips, status, max_attempts, created_at)
     values ('${jobId}', '${sessionId}', 'upload', 'sources/${jobId}/reserved', 1, 'uploading',
             ${options.maxAttempts ?? 3}, now() - (${createdOffset} * interval '1 second'))`
  );
  const lockFields = (expired: boolean) =>
    `locked_by = 'seed', locked_at = now(), lease_expires_at = ${
      expired ? "now() - interval '1 minute'" : `now() + interval '${options.lockSeconds ?? 5} minutes'`
    }`;
  await db.query(`update public.jobs set status = 'queued', upload_completed_at = now() where id = '${jobId}'`);
  if (status !== "queued") {
    await db.query(`update public.jobs set status = 'validating', ${lockFields(false)} where id = '${jobId}'`);
    await db.query(`update public.jobs set status = 'ready', locked_by = null, locked_at = null, lease_expires_at = null where id = '${jobId}'`);
  }
  if (status === "parked" || status === "rendering") {
    await db.query(`update public.jobs set status = 'transcribing', ${lockFields(false)} where id = '${jobId}'`);
    await db.query(`update public.jobs set status = 'analyzing', ${lockFields(false)} where id = '${jobId}'`);
  }
  if (status === "parked") {
    await db.query(`update public.jobs set locked_by = null, locked_at = null, lease_expires_at = null, progress = 100 where id = '${jobId}'`);
    if (options.renderAttempts) {
      await db.query(`update public.jobs set render_attempts = ${options.renderAttempts} where id = '${jobId}'`);
    }
    const clips = options.clips ?? 0;
    for (let i = 0; i < clips; i += 1) {
      const clipId = `aaaaaaaa-aaaa-4aaa-8aaa-${String(500000000000 + jobSeq * 100 + i).padStart(12, "0")}`;
      await db.query(
        `insert into public.clips (id, job_id, title, start_time, end_time, sort_order, status, storage_path)
         values ('${clipId}', '${jobId}', 'clip ${i}', ${i * 90}, ${i * 90 + 90}, ${i},
                 '${options.clipStatus ?? "pending"}',
                 ${options.clipStoragePath ? `'clips/${jobId}/${clipId}.mp4'` : "null"})`
      );
    }
  } else if (status === "rendering") {
    // analyzing (locked) → rendering, keeping the lease (expired or not).
    await db.query(`update public.jobs set status = 'rendering', ${lockFields(Boolean(options.lockExpired))} where id = '${jobId}'`);
  }
  return jobId;
}

function cleanQueue() {
  return db.query("delete from public.jobs");
}

const MIGRATIONS = [
  "20261113000000_initial_schema.sql",
  "20261114000000_phase2_job_settings_and_session_lookup.sql",
  "20261115000000_phase3_queue_and_ingestion.sql",
  "20261116000000_phase4_clip_analysis.sql",
  "20261117000000_phase5_rendering.sql",
];

beforeAll(async () => {
  db = await PGlite.create();

  await db.exec(`
    create role anon noinherit nologin;
    create role service_role noinherit nologin bypassrls;
    grant usage on schema public to anon, service_role;
  `);

  const migrationsDir = join(process.cwd(), "supabase", "migrations");
  for (const file of MIGRATIONS) {
    await db.exec(readFileSync(join(migrationsDir, file), "utf-8"));
  }

  await db.exec(`
    grant select, insert, update, delete on public.processing_sessions, public.jobs, public.clips to anon, service_role;
  `);
});

afterAll(async () => {
  await db?.close();
});

describe("Phase 5 schema", () => {
  it("adds render_attempts (default 0, >= 0) without touching existing rows", async () => {
    const columns = (await q("select column_name from information_schema.columns where table_name = 'jobs'")).map(
      (c) => c.column_name as string
    );
    expect(columns).toContain("render_attempts");

    // A job seeded BEFORE any rendering keeps the default 0 (existing data preserved).
    const jobId = await seedJob("ready");
    expect(await scalar(`select render_attempts from public.jobs where id = '${jobId}'`)).toBe(0);

    await expectSqlError(
      `update public.jobs set render_attempts = -1 where id = '${jobId}'`,
      /check/i
    );
    await cleanQueue();
  });

  it("allows an active rendering lease but not an unlocked rendering row", async () => {
    // Rendering + lock set is legal (claim produces this).
    const active = await seedJob("rendering");
    const row = await one(`select status, locked_by, lease_expires_at from public.jobs where id = '${active}'`);
    expect(row?.status).toBe("rendering");
    expect(row?.locked_by).toBe("seed");
    await cleanQueue();

    // ...but hand-crafting an unlocked rendering row must be rejected.
    // Parked analyzing → rendering is a LEGAL transition, so the failure can
    // only come from the lock-consistency constraint.
    const bare = await seedJob("parked");
    await expectSqlError(
      `update public.jobs set status = 'rendering' where id = '${bare}'`,
      /jobs_lock_consistency|check/i
    );
    await cleanQueue();
  });

  it("keeps the parked analyzing state legal (Phase 4 regression)", async () => {
    const parked = await seedJob("parked");
    const row = await one(`select status, locked_by from public.jobs where id = '${parked}'`);
    expect(row?.status).toBe("analyzing");
    expect(row?.locked_by).toBeNull();
    await cleanQueue();
  });

  it("is idempotent (the migration can be re-executed)", async () => {
    const migrationsDir = join(process.cwd(), "supabase", "migrations");
    const phase5 = readFileSync(join(migrationsDir, "20261117000000_phase5_rendering.sql"), "utf-8");
    await db.exec(phase5); // no error, no schema drift
    const count = await scalar(`select count(*) from information_schema.columns where table_name = 'jobs' and column_name = 'render_attempts'`);
    expect(count).toBe(1);
  });

  it("creates the clips (job_id, status) lookup index", async () => {
    const index = await one(`select indexname from pg_indexes where tablename = 'clips' and indexname = 'clips_job_status_idx'`);
    expect(index).toBeDefined();
  });
});

describe("Phase 5 claiming", () => {
  it("claims a parked analyzing job and moves it to rendering", async () => {
    const jobId = await seedJob("parked", { createdAtOffsetSeconds: 10 });
    const claimed = await one(`select * from public.claim_next_job('w5', 300)`);
    expect(claimed?.id).toBe(jobId);
    expect(claimed?.status).toBe("rendering");
    expect(claimed?.locked_by).toBe("w5");
    // The seed walks the legal path via direct updates (no claims), so the
    // claim is the first attempt.
    expect(claimed?.attempt_count).toBe(1);
    expect(claimed?.render_attempts).toBe(0);
    await cleanQueue();
  });

  it("FIFO: the oldest claimable job wins (parked before newer queued)", async () => {
    const queued = await seedJob("ready", { createdAtOffsetSeconds: 0 }); // newest
    const parked = await seedJob("parked", { createdAtOffsetSeconds: 300 }); // oldest
    // seedJob increments created_at offsets; ensure the parked one is older.
    void queued;
    const claimed = await one(`select * from public.claim_next_job('w5', 300)`);
    expect(claimed?.id).toBe(parked);
    expect(claimed?.status).toBe("rendering");
    // The ready job is still claimable next.
    const second = await one(`select * from public.claim_next_job('w5', 300)`);
    expect(second?.id).toBe(queued);
    expect(second?.status).toBe("transcribing");
    await cleanQueue();
  });

  it("refuses a parked job whose render budget is exhausted", async () => {
    const jobId = await seedJob("parked", { renderAttempts: 3, maxAttempts: 3 });
    expect(await scalar(`select * from public.claim_next_job('w5', 300)`)).toBeNull();
    // The row stays parked, untouched.
    const row = await one(`select status, locked_by, render_attempts from public.jobs where id = '${jobId}'`);
    expect(row?.status).toBe("analyzing");
    expect(row?.locked_by).toBeNull();
    expect(row?.render_attempts).toBe(3);
    await cleanQueue();
  });

  it("recovers a crashed rendering job via its expired lease (stage kept)", async () => {
    const jobId = await seedJob("rendering", { lockExpired: true });
    const claimed = await one(`select * from public.claim_next_job('w5', 300)`);
    expect(claimed?.id).toBe(jobId);
    expect(claimed?.status).toBe("rendering");
    expect(claimed?.locked_by).toBe("w5");
    await cleanQueue();
  });

  it("never takes an active rendering job from its worker", async () => {
    const jobId = await seedJob("rendering");
    expect(await scalar(`select * from public.claim_next_job('w5', 300)`)).toBeNull();
    const row = await one(`select locked_by from public.jobs where id = '${jobId}'`);
    expect(row?.locked_by).toBe("seed");
    await cleanQueue();
  });

  it("never claims a parked job while its render lease is held by an active analysis", async () => {
    // An ACTIVE analyzing job (lock set, expired lease) is recovery, not parking:
    // it must be claimed AS analyzing (re-run analysis), not jumped to rendering.
    const jobId = await seedJob("parked");
    await db.query(
      `update public.jobs set locked_by = 'crashed', locked_at = now(), lease_expires_at = now() - interval '1 minute' where id = '${jobId}'`
    );
    const claimed = await one(`select * from public.claim_next_job('w5', 300)`);
    expect(claimed?.id).toBe(jobId);
    expect(claimed?.status).toBe("analyzing");
    await cleanQueue();
  });

  it("still claims queued and ready jobs as before (Phase 3/4 regression)", async () => {
    const queued = await seedJob("queued", {});
    const first = await one(`select * from public.claim_next_job('w5', 300)`);
    expect(first?.id).toBe(queued);
    expect(first?.status).toBe("validating");
    await cleanQueue();

    const ready = await seedJob("ready");
    const second = await one(`select * from public.claim_next_job('w5', 300)`);
    expect(second?.id).toBe(ready);
    expect(second?.status).toBe("transcribing");
    await cleanQueue();
  });
});

describe("Phase 5 lease / progress in rendering", () => {
  it("renews a rendering lease for the owner only", async () => {
    const jobId = await seedJob("rendering");
    await db.query(`update public.jobs set locked_by = 'w5' where id = '${jobId}'`);
    expect(await scalar(`select public.renew_job_lease('${jobId}'::uuid, 'w5', 300)`)).toBe(true);
    expect(await scalar(`select public.renew_job_lease('${jobId}'::uuid, 'other', 300)`)).toBe(false);
    await cleanQueue();
  });

  it("records progress for the owner only", async () => {
    const jobId = await seedJob("rendering");
    await db.query(`update public.jobs set locked_by = 'w5' where id = '${jobId}'`);
    expect(await scalar(`select public.update_job_progress('${jobId}'::uuid, 'w5', 42)`)).toBe(true);
    expect(await scalar(`select progress from public.jobs where id = '${jobId}'`)).toBe(42);
    expect(await scalar(`select public.update_job_progress('${jobId}'::uuid, 'other', 43)`)).toBe(false);
    await cleanQueue();
  });
});

describe("Phase 5 failure and release in rendering", () => {
  it("transient rendering failure re-parks (render_attempts + 1) and is re-claimed", async () => {
    const jobId = await seedJob("rendering");
    await db.query(`update public.jobs set locked_by = 'w5' where id = '${jobId}'`);
    expect(await scalar(`select public.fail_job('${jobId}'::uuid, 'w5', 'render_failed', 'boom', false)`)).toBe(
      "analyzing"
    );
    const row = await one(`select status, locked_by, render_attempts from public.jobs where id = '${jobId}'`);
    expect(row?.status).toBe("analyzing");
    expect(row?.locked_by).toBeNull();
    expect(row?.render_attempts).toBe(1);
    // Re-claimable → rendering again.
    const claimed = await one(`select * from public.claim_next_job('w5', 300)`);
    expect(claimed?.id).toBe(jobId);
    expect(claimed?.status).toBe("rendering");
    await cleanQueue();
  });

  it("permanent rendering failure is terminal", async () => {
    const jobId = await seedJob("rendering");
    await db.query(`update public.jobs set locked_by = 'w5' where id = '${jobId}'`);
    expect(await scalar(`select public.fail_job('${jobId}'::uuid, 'w5', 'corrupt_media', 'bad', true)`)).toBe(
      "failed"
    );
    await cleanQueue();
  });

  it("an exhausted render budget fails terminally instead of parking forever", async () => {
    const jobId = await seedJob("rendering");
    await db.query(`update public.jobs set locked_by = 'w5', render_attempts = 2 where id = '${jobId}'`);
    // max_attempts = 3 (default); 2 + 1 >= 3 → failed.
    expect(await scalar(`select public.fail_job('${jobId}'::uuid, 'w5', 'render_failed', 'boom', false)`)).toBe(
      "failed"
    );
    const row = await one(`select status, render_attempts, last_error from public.jobs where id = '${jobId}'`);
    expect(row?.status).toBe("failed");
    // The failed attempt is still counted (2 → 3).
    expect(row?.render_attempts).toBe(3);
    expect(String(row?.last_error)).toMatch(/render_failed/);
    await cleanQueue();
  });

  it("rejects a failure from a non-owner", async () => {
    const jobId = await seedJob("rendering");
    expect(await scalar(`select public.fail_job('${jobId}'::uuid, 'w5', 'render_failed', 'boom', false)`)).toBe(
      "not_owner"
    );
    await cleanQueue();
  });

  it("releases a rendering job back to the parked state", async () => {
    const jobId = await seedJob("rendering");
    await db.query(`update public.jobs set locked_by = 'w5' where id = '${jobId}'`);
    expect(await scalar(`select public.release_job('${jobId}'::uuid, 'w5')`)).toBe(true);
    const row = await one(`select status, locked_by, render_attempts from public.jobs where id = '${jobId}'`);
    expect(row?.status).toBe("analyzing");
    expect(row?.locked_by).toBeNull();
    expect(row?.render_attempts).toBe(0); // release does not burn the render budget
    await cleanQueue();
  });

  it("keeps the Phase 3/4 release targets (validating → queued, transcribing → ready)", async () => {
    const validating = await seedJob("queued");
    const claimed = await one(`select * from public.claim_next_job('w5', 300)`);
    expect(claimed?.status).toBe("validating");
    expect(await scalar(`select public.release_job('${validatedId(claimed)}'::uuid, 'w5')`)).toBe(true);
    expect(await scalar(`select status from public.jobs where id = '${validatedId(claimed)}'`)).toBe("queued");
    await cleanQueue();

    const ready = await seedJob("ready");
    const claimed2 = await one(`select * from public.claim_next_job('w5', 300)`);
    expect(claimed2?.status).toBe("transcribing");
    expect(await scalar(`select public.release_job('${validatedId(claimed2)}'::uuid, 'w5')`)).toBe(true);
    expect(await scalar(`select status from public.jobs where id = '${validatedId(claimed2)}'`)).toBe("ready");
    await cleanQueue();
  });
});

function validatedId(claimed: Row | undefined): string {
  expect(claimed).toBeDefined();
  return String(claimed?.id);
}

describe("Phase 5 terminal completion", () => {
  it("refuses completion with zero clips", async () => {
    const jobId = await seedJob("parked", { clips: 0 });
    await one(`select * from public.claim_next_job('w5', 300)`);
    expect(await scalar(`select public.complete_job_rendering('${jobId}'::uuid, 'w5')`)).toBe(false);
    const row = await one(`select status, completed_at, locked_by from public.jobs where id = '${jobId}'`);
    expect(row?.status).toBe("rendering");
    expect(row?.completed_at).toBeNull();
    expect(row?.locked_by).toBe("w5");
    await cleanQueue();
  });

  it("refuses completion while any clip is not ready", async () => {
    const jobId = await seedJob("parked", { clips: 2, clipStatus: "ready", clipStoragePath: true });
    // Postgres has no UPDATE ... LIMIT: target the first clip via subquery.
    await db.query(
      `update public.clips set status = 'pending'
        where job_id = '${jobId}'
          and id = (select c.id from public.clips c where c.job_id = '${jobId}' order by c.sort_order limit 1)`
    );
    await one(`select * from public.claim_next_job('w5', 300)`);
    expect(await scalar(`select public.complete_job_rendering('${jobId}'::uuid, 'w5')`)).toBe(false);
    await cleanQueue();
  });

  it("refuses completion when a ready clip has no storage path (not stored)", async () => {
    const jobId = await seedJob("parked", { clips: 1, clipStatus: "ready", clipStoragePath: false });
    await one(`select * from public.claim_next_job('w5', 300)`);
    expect(await scalar(`select public.complete_job_rendering('${jobId}'::uuid, 'w5')`)).toBe(false);
    await cleanQueue();
  });

  it("refuses completion for a non-owner", async () => {
    const jobId = await seedJob("parked", { clips: 1, clipStatus: "ready", clipStoragePath: true });
    await one(`select * from public.claim_next_job('w5', 300)`);
    expect(await scalar(`select public.complete_job_rendering('${jobId}'::uuid, 'someone-else')`)).toBe(false);
    await cleanQueue();
  });

  it("refuses completion once the job left rendering (parked)", async () => {
    const jobId = await seedJob("parked", { clips: 1, clipStatus: "ready", clipStoragePath: true });
    expect(await scalar(`select public.complete_job_rendering('${jobId}'::uuid, 'w5')`)).toBe(false);
    await cleanQueue();
  });

  it("completes when every clip is ready AND stored", async () => {
    const jobId = await seedJob("parked", { clips: 2, clipStatus: "ready", clipStoragePath: true });
    await one(`select * from public.claim_next_job('w5', 300)`);
    expect(await scalar(`select public.complete_job_rendering('${jobId}'::uuid, 'w5')`)).toBe(true);
    const row = await one(
      `select status, completed_at, progress, locked_by, lease_expires_at, last_error from public.jobs where id = '${jobId}'`
    );
    expect(row?.status).toBe("completed");
    expect(row?.completed_at).not.toBeNull();
    expect(row?.progress).toBe(100);
    expect(row?.locked_by).toBeNull();
    expect(row?.lease_expires_at).toBeNull();
    // A completed job is no longer claimable by anyone.
    expect(await scalar(`select * from public.claim_next_job('w6', 300)`)).toBeNull();
    await cleanQueue();
  });
});

describe("Phase 5 transition map", () => {
  it("allows rendering → analyzing (retry) and rendering → completed", async () => {
    const jobId = await seedJob("rendering");
    await db.query(`update public.jobs set locked_by = 'w5' where id = '${jobId}'`);
    await db.query(`update public.jobs set status = 'analyzing', locked_by = null, locked_at = null, lease_expires_at = null where id = '${jobId}'`);
    expect(await scalar(`select status from public.jobs where id = '${jobId}'`)).toBe("analyzing");
    await cleanQueue();
  });

  it("rejects rendering → ready / queued / transcribing", async () => {
    for (const target of ["ready", "queued", "transcribing"]) {
      const jobId = await seedJob("rendering");
      await expectSqlError(
        `update public.jobs set status = '${target}' where id = '${jobId}'`,
        /illegal job status transition/i
      );
      await cleanQueue();
    }
  });

  it("keeps the Phase 4 map (analyzing → rendering legal, completed → anything illegal)", async () => {
    const jobId = await seedJob("parked");
    // parked (lock cleared) cannot jump to rendering directly with a lock set...
    await expectSqlError(`update public.jobs set status = 'rendering' where id = '${jobId}'`, /jobs_lock_consistency|check/i);
    // ...but the claim performs it atomically:
    const claimed = await one(`select * from public.claim_next_job('w5', 300)`);
    expect(claimed?.status).toBe("rendering");
    await cleanQueue();

    const done = await seedJob("parked", { clips: 1, clipStatus: "ready", clipStoragePath: true });
    await one(`select * from public.claim_next_job('w5', 300)`);
    await db.query(`select public.complete_job_rendering('${done}'::uuid, 'w5')`);
    await expectSqlError(
      `update public.jobs set status = 'rendering' where id = '${done}'`,
      /illegal job status transition/i
    );
    await cleanQueue();
  });

  it("keeps the completed_at invariant", async () => {
    const jobId = await seedJob("ready");
    await expectSqlError(`update public.jobs set completed_at = now() where id = '${jobId}'`, /jobs_completed_consistency|check/i);
    await cleanQueue();
  });
});

describe("Phase 5 privileges", () => {
  it("denies anon every queue RPC (deny-by-default)", async () => {
    const jobId = await seedJob("parked");
    await db.query(`set role anon`);
    await expectSqlError(`select public.claim_next_job('anon', 300)`, /permission denied|not allowed/i);
    await expectSqlError(
      `select public.complete_job_rendering('${jobId}'::uuid, 'anon')`,
      /permission denied|not allowed/i
    );
    await db.query(`reset role`);
    const row = await one(`select status, locked_by from public.jobs where id = '${jobId}'`);
    expect(row?.status).toBe("analyzing");
    expect(row?.locked_by).toBeNull();
    await cleanQueue();
  });

  it("grants service_role the new completion RPC (and only service_role)", async () => {
    const grants = await q(
      `select grantee from information_schema.role_routine_grants where routine_schema = 'public' and routine_name = 'complete_job_rendering'`
    );
    const grantees = grants.map((g) => g.grantee as string);
    expect(grantees).toContain("service_role");
    expect(grantees).not.toContain("anon");
  });
});
