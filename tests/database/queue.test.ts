/**
 * Phase 3 — database queue tests against a REAL PostgreSQL engine.
 *
 * The exact Phase 3 migration (queue columns, 9-state constraint, transition
 * trigger, restricted RPCs) is executed on @electric-sql/pglite (Postgres 16
 * WASM) and the worker-facing contract is verified:
 *
 *   - atomic claiming (two workers never get the same active job)
 *   - lease expiration → recovery by another worker
 *   - bounded retries (attempt_count vs max_attempts)
 *   - lease/progress/complete/fail/release all scoped to the claiming worker
 *   - state transitions enforced by the database trigger
 *   - RPCs revoked from anon, granted to service_role only
 *   - worker never sees 'completed' (ingestion ends at 'ready')
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { PGlite } from "@electric-sql/pglite";

type Db = InstanceType<typeof PGlite>;
type Row = Record<string, unknown>;

let db: Db;

async function q(sql: string): Promise<Row[]> {
  const result = await db.query(sql);
  return result.rows as Row[];
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

/** Inserts a session + upload job; returns the job id. */
async function seedJob(overrides: {
  status?: string;
  maxAttempts?: number;
  attemptCount?: number;
  createdAtOffsetSeconds?: number;
} = {}): Promise<string> {
  sessionSeq += 1;
  jobSeq += 1;
  const sessionId = `44444444-4444-4444-8444-${String(444444444000 + sessionSeq).padStart(12, "0")}`;
  const jobId = `55555555-5555-4555-8555-${String(555555555000 + jobSeq).padStart(12, "0")}`;
  const status = overrides.status ?? "uploading";
  const createdOffset = overrides.createdAtOffsetSeconds ?? 0;
  await db.query(
    `insert into public.processing_sessions (id, token_hash, expires_at) values ('${sessionId}', 'q-${jobSeq}', now() + interval '1 hour')`
  );
  await db.query(
    `insert into public.jobs
        (id, session_id, source_type, source_path, requested_clips, status, max_attempts, attempt_count, created_at)
     values ('${jobId}', '${sessionId}', 'upload', 'sources/${jobId}/reserved', 1, '${status}',
             ${overrides.maxAttempts ?? 3}, ${overrides.attemptCount ?? 0},
             now() - (${createdOffset} * interval '1 second'))`
  );
  if (status === "uploading") {
    await db.query(
      `update public.jobs set status = 'queued', upload_completed_at = now() where id = '${jobId}'`
    );
  }
  return jobId;
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

describe("queue schema", () => {
  it("adds the queue/lease/ingestion columns", async () => {
    const columns = await q(
      "select column_name from information_schema.columns where table_name = 'jobs' order by 1"
    );
    const names = columns.map((c) => c.column_name as string);
    for (const expected of [
      "attempt_count",
      "max_attempts",
      "locked_at",
      "locked_by",
      "lease_expires_at",
      "last_error",
      "upload_completed_at",
      "upload_auth_expires_at",
      "source_metadata",
      "updated_at",
    ]) {
      expect(names, expected).toContain(expected);
    }
  });

  it("widens the status constraint to the nine-state machine", async () => {
    await db.query(
      "insert into public.processing_sessions (id, token_hash, expires_at) values ('66666666-6666-4666-8666-666666666601', 'q-states', now() + interval '1 hour')"
    );
    const sessionId = "66666666-6666-4666-8666-666666666601";
    await expectSqlError(
      `insert into public.jobs (session_id, source_type, source_url, requested_clips, status) values ('${sessionId}', 'youtube', 'https://youtube.com/watch?v=x', 1, 'stillborn')`,
      /check/i
    );
    // 'ready' (a Phase 3-only state) is accepted by the widened constraint.
    const id = (await scalar(
      `insert into public.jobs (session_id, source_type, source_path, requested_clips, status) values ('${sessionId}', 'upload', 'sources/x/reserved', 1, 'ready') returning id::text`
    )) as string;
    expect(id).toBeTruthy();
  });

  it("enforces lock-field consistency (locked_* only while validating)", async () => {
    const jobId = await seedJob();
    await expectSqlError(
      `update public.jobs set locked_by = 'w1' where id = '${jobId}'`,
      /jobs_lock_consistency|check/i
    );
  });
});

describe("atomic claiming (service_role)", () => {
  // claim_next_job picks the OLDEST claimable job in the whole table —
  // every test starts from a clean queue to stay deterministic.
  async function cleanQueue() {
    await db.query("delete from public.jobs");
  }

  it("claims the oldest queued job and sets the lock fields", async () => {
    await cleanQueue();
    const older = await seedJob({ createdAtOffsetSeconds: 60 });
    const newer = await seedJob({ createdAtOffsetSeconds: 10 });

    await db.query("set role service_role");
    const claimed = (await one(
      "select id::text as id, status, attempt_count, locked_by, (locked_at is not null) as has_locked_at, (lease_expires_at > now()) as lease_future from public.claim_next_job('w1', 120)"
    )) as Row;
    await db.query("set role postgres");

    expect(claimed?.id).toBe(older); // oldest first
    expect(claimed?.status).toBe("validating");
    expect(claimed?.attempt_count).toBe(1);
    expect(claimed?.locked_by).toBe("w1");
    expect(claimed?.has_locked_at).toBe(true);
    expect(claimed?.lease_future).toBe(true);
  });

  it("a second worker claims the other job (never the same active job)", async () => {
    await cleanQueue();
    const a = await seedJob({ createdAtOffsetSeconds: 5 });
    const b = await seedJob({ createdAtOffsetSeconds: 1 });

    await db.query("set role service_role");
    const first = (await one("select id::text as id from public.claim_next_job('w1', 120)")) as Row;
    expect(first?.id).toBe(a); // oldest first

    const remaining = (await scalar(
      "select count(*)::int from public.jobs where status = 'queued'"
    )) as number;
    expect(remaining).toBe(1);

    const second = (await one("select id::text as id, locked_by from public.claim_next_job('w2', 120)")) as Row;
    await db.query("set role postgres");
    expect(second?.id).toBe(b);
    expect(second?.locked_by).toBe("w2");
  });

  it("returns no job when nothing is claimable", async () => {
    await cleanQueue();
    await db.query("set role service_role");
    const claimed = (await one("select id::text as id from public.claim_next_job('w9', 120)")) as Row;
    await db.query("set role postgres");
    expect(claimed?.id ?? null).toBeNull();
  });

  it("does not re-claim a job whose lease is still active", async () => {
    await cleanQueue();
    const jobId = await seedJob();
    await db.query("set role service_role");
    const first = (await one(`select id::text as id from public.claim_next_job('wa', 3600)`)) as Row;
    expect(first?.id).toBe(jobId);
    const second = (await one(`select id::text as id from public.claim_next_job('wb', 120)`)) as Row;
    await db.query("set role postgres");
    expect(second?.id ?? null).toBeNull();
  });

  it("recovers a job whose lease expired (crash recovery)", async () => {
    await cleanQueue();
    const jobId = await seedJob();
    await db.query("set role service_role");
    const first = (await one(
      "select id::text as id, attempt_count from public.claim_next_job('wc', 30)"
    )) as Row;
    expect(first?.id).toBe(jobId);
    expect(first?.attempt_count).toBe(1);

    // Simulate the worker crashing: the lease lapses while the job is stuck in 'validating'.
    await db.query(`update public.jobs set lease_expires_at = now() - interval '1 minute' where id = '${jobId}'`);
    const recovered = (await one(
      "select id::text as id, attempt_count, locked_by from public.claim_next_job('wd', 30)"
    )) as Row;
    await db.query("set role postgres");
    expect(recovered?.id).toBe(jobId);
    expect(recovered?.locked_by).toBe("wd");
    expect(recovered?.attempt_count).toBe(2);
  });

  it("never claims a job that exhausted its attempt budget", async () => {
    await cleanQueue();
    const jobId = await seedJob({ maxAttempts: 2, attemptCount: 2 });
    await db.query("set role service_role");
    const claimed = (await one("select id::text as id from public.claim_next_job('we', 120)")) as Row;
    await db.query("set role postgres");
    expect(claimed?.id ?? null).toBeNull();
    const status = await scalar(`select status from public.jobs where id = '${jobId}'`);
    expect(status).toBe("queued");
  });

  it("rejects invalid worker ids and lease durations", async () => {
    await db.query("set role service_role");
    await expectSqlError("select public.claim_next_job('', 120)", /invalid worker id/i);
    await expectSqlError("select public.claim_next_job('wf', 29)", /between 30 and 3600/i);
    await db.query("set role postgres");
  });
});

describe("lease ownership & progress (scoped to the claiming worker)", () => {
  async function claimFreshJob(worker: string): Promise<string> {
    await db.query("delete from public.jobs");
    const jobId = await seedJob();
    await db.query("set role service_role");
    const claimed = (await one(`select id::text as id from public.claim_next_job('${worker}', 120)`)) as Row;
    await db.query("set role postgres");
    expect(claimed?.id).toBe(jobId);
    return jobId;
  }

  it("the owner can renew the lease; a stranger cannot", async () => {
    const jobId = await claimFreshJob("w-own");
    const before = (await scalar(`select lease_expires_at from public.jobs where id = '${jobId}'`)) as string;

    await db.query("set role service_role");
    const stranger = (await scalar(
      `select public.renew_job_lease('${jobId}'::uuid, 'w-other', 300)`
    )) as boolean;
    expect(stranger).toBe(false);

    const owner = (await scalar(
      `select public.renew_job_lease('${jobId}'::uuid, 'w-own', 300)`
    )) as boolean;
    const after = (await scalar(`select lease_expires_at from public.jobs where id = '${jobId}'`)) as string;
    await db.query("set role postgres");
    expect(owner).toBe(true);
    expect(new Date(after).getTime()).toBeGreaterThan(new Date(before).getTime());
  });

  it("progress updates only stick for the owning worker", async () => {
    const jobId = await claimFreshJob("w-prog");
    await db.query("set role service_role");
    const stranger = (await scalar(
      `select public.update_job_progress('${jobId}'::uuid, 'w-x', 50)`
    )) as boolean;
    expect(stranger).toBe(false);
    const owner = (await scalar(
      `select public.update_job_progress('${jobId}'::uuid, 'w-prog', 55)`
    )) as boolean;
    await db.query("set role postgres");
    expect(owner).toBe(true);
    expect(await scalar(`select progress from public.jobs where id = '${jobId}'`)).toBe(55);
  });

  it("rejects out-of-range progress values", async () => {
    const jobId = await claimFreshJob("w-range");
    await db.query("set role service_role");
    await expectSqlError(`select public.update_job_progress('${jobId}'::uuid, 'w-range', 101)`, /between 0 and 100/i);
    await db.query("set role postgres");
  });

  it("complete_job_ingestion: owner finishes at 'ready' with merged metadata and a cleared lock", async () => {
    const jobId = await claimFreshJob("w-done");
    await db.query("set role service_role");
    const stranger = (await scalar(
      `select public.complete_job_ingestion('${jobId}'::uuid, 'w-nope', '{"probe":"stranger"}'::jsonb)`
    )) as boolean;
    expect(stranger).toBe(false);

    const done = (await scalar(
      `select public.complete_job_ingestion('${jobId}'::uuid, 'w-done', '{"container":"mov,mp4,m4a,3gp,3g2,mj2","duration_seconds":42.5,"width":1080,"height":1920,"video_codec":"h264","has_audio":true,"audio_codec":"aac","file_size_bytes":1234}'::jsonb)`
    )) as boolean;
    await db.query("set role postgres");
    expect(done).toBe(true);

    const row = (await one(
      `select status, progress, locked_by, lease_expires_at, source_metadata from public.jobs where id = '${jobId}'`
    )) as Row;
    expect(row?.status).toBe("ready");
    expect(row?.progress).toBe(100);
    expect(row?.locked_by ?? null).toBeNull();
    expect(row?.lease_expires_at ?? null).toBeNull();
    const meta = row?.source_metadata as Record<string, unknown>;
    // Probe facts are merged into the verified source metadata.
    expect(meta.container).toBe("mov,mp4,m4a,3gp,3g2,mj2");
    expect(meta.duration_seconds).toBe(42.5);
    expect(meta.video_codec).toBe("h264");
  });
});

describe("failure handling (bounded retries, safe categories)", () => {
  async function claimFreshJob(worker: string, opts: { maxAttempts?: number; attemptCount?: number } = {}): Promise<string> {
    await db.query("delete from public.jobs");
    const jobId = await seedJob(opts);
    await db.query("set role service_role");
    const claimed = (await one(`select id::text as id from public.claim_next_job('${worker}', 120)`)) as Row;
    await db.query("set role postgres");
    expect(claimed?.id).toBe(jobId);
    return jobId;
  }

  it("a transient failure requeues the job (attempt budget respected)", async () => {
    const jobId = await claimFreshJob("w-retry"); // attempt_count now 1 < 3
    await db.query("set role service_role");
    const newStatus = (await scalar(
      `select public.fail_job('${jobId}'::uuid, 'w-retry', 'storage_download', 'network timeout', false)`
    )) as string;
    const row = (await one(
      `select status, attempt_count, locked_by, last_error from public.jobs where id = '${jobId}'`
    )) as Row;
    await db.query("set role postgres");
    expect(newStatus).toBe("queued");
    expect(row?.status).toBe("queued");
    expect(row?.attempt_count).toBe(1); // preserved — claim will increment again
    expect(row?.locked_by ?? null).toBeNull();
    expect(String(row?.last_error ?? "")).toMatch(/^storage_download: /);
  });

  it("a permanent failure goes straight to 'failed' without using the retry budget", async () => {
    const jobId = await claimFreshJob("w-perm");
    await db.query("set role service_role");
    const newStatus = (await scalar(
      `select public.fail_job('${jobId}'::uuid, 'w-perm', 'corrupt_media', 'invalid container', true)`
    )) as string;
    const row = (await one(`select status, error_message, last_error from public.jobs where id = '${jobId}'`)) as Row;
    await db.query("set role postgres");
    expect(newStatus).toBe("failed");
    expect(row?.status).toBe("failed");
    expect(String(row?.last_error ?? "")).toMatch(/^corrupt_media: /);
  });

  it("a transient failure at max attempts terminates at 'failed'", async () => {
    const jobId = await claimFreshJob("w-max", { maxAttempts: 2, attemptCount: 1 }); // claim → 2 = max
    await db.query("set role service_role");
    const newStatus = (await scalar(
      `select public.fail_job('${jobId}'::uuid, 'w-max', 'ffprobe_failed', 'probe crashed', false)`
    )) as string;
    await db.query("set role postgres");
    expect(newStatus).toBe("failed");
  });

  it("a non-owner cannot fail the job (not_owner, row untouched)", async () => {
    const jobId = await claimFreshJob("w-guard");
    await db.query("set role service_role");
    const result = (await scalar(
      `select public.fail_job('${jobId}'::uuid, 'w-intruder', 'unknown', 'hack', false)`
    )) as string;
    const row = (await one(`select status, locked_by, last_error from public.jobs where id = '${jobId}'`)) as Row;
    await db.query("set role postgres");
    expect(result).toBe("not_owner");
    expect(row?.status).toBe("validating");
    expect(row?.locked_by).toBe("w-guard");
    expect(row?.last_error ?? null).toBeNull();
  });

  it("release_job returns the job to the queue only for its owner", async () => {
    const jobId = await claimFreshJob("w-rel");
    await db.query("set role service_role");
    const stranger = (await scalar(`select public.release_job('${jobId}'::uuid, 'w-x')`)) as boolean;
    expect(stranger).toBe(false);
    const owner = (await scalar(`select public.release_job('${jobId}'::uuid, 'w-rel')`)) as boolean;
    await db.query("set role postgres");
    expect(owner).toBe(true);
    const row = (await one(`select status, locked_by from public.jobs where id = '${jobId}'`)) as Row;
    expect(row?.status).toBe("queued");
    expect(row?.locked_by ?? null).toBeNull();
  });
});

describe("state machine enforcement & updated_at", () => {
  it("rejects an illegal status transition (queued → ready)", async () => {
    const jobId = await seedJob();
    await expectSqlError(
      `update public.jobs set status = 'ready' where id = '${jobId}'`,
      /illegal job status transition queued -> ready/i
    );
  });

  it("allows the legal ingestion path (queued → validating → ready)", async () => {
    const jobId = await seedJob();
    await db.query(`update public.jobs set status = 'validating', locked_by = 'wx', locked_at = now(), lease_expires_at = now() + interval '5 minutes' where id = '${jobId}'`);
    await db.query(`update public.jobs set status = 'ready', locked_by = null, locked_at = null, lease_expires_at = null where id = '${jobId}'`);
    const status = await scalar(`select status from public.jobs where id = '${jobId}'`);
    expect(status).toBe("ready");
  });

  it("maintains updated_at on job updates", async () => {
    const jobId = await seedJob();
    const before = (await scalar(`select updated_at from public.jobs where id = '${jobId}'`)) as string;
    await db.query("set role service_role");
    await db.query(`update public.jobs set progress = 5 where id = '${jobId}'`);
    const after = (await scalar(`select updated_at from public.jobs where id = '${jobId}'`)) as string;
    await db.query("set role postgres");
    expect(new Date(after).getTime()).toBeGreaterThanOrEqual(new Date(before).getTime());
  });
});

describe("RPC access control", () => {
  it("denies queue-control RPCs to the anon role", async () => {
    await db.query("set role anon");
    await expectSqlError("select public.claim_next_job('anon-w', 120)", /permission denied/i);
    await db.query("set role postgres");
  });

  it("keeps anon reads empty (RLS) even though the RPCs exist", async () => {
    await db.query("set role anon");
    const count = (await scalar("select count(*)::int from public.jobs")) as number;
    expect(count).toBe(0);
    await db.query("set role postgres");
  });
});
