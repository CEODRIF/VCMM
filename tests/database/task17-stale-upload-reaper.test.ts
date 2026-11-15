/**
 * TASK 017 — stale-upload reaper RPC (`reap_stale_uploading_jobs`) on a REAL
 * Postgres engine (PGlite, Postgres 16 wasm).
 *
 * A job stuck in 'uploading' past its upload-authorization TTL is a zombie:
 * claim_next_job never takes it and finalization becomes impossible. The
 * reaper (worker, idle cycles) fails such jobs TERMINALLY — which also arms
 * the Phase 6 retention deadline on any partially uploaded object.
 *
 * Proven here (executed, not inspected):
 *   - only STALE 'uploading' rows are failed; fresh uploads and every other
 *     status (queued/rendering) are untouched,
 *   - the failed row is terminal, lock-free, carries a user-facing
 *     last_error, and gets a retention deadline (expires_at),
 *   - the RPC is idempotent (second run reaps nothing),
 *   - the age threshold is validated server-side (min 60 s),
 *   - the function is SECURITY DEFINER with EXECUTE for service_role only
 *     (anon gets nothing) — same trust model as the queue RPCs.
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { PGlite } from "@electric-sql/pglite";

const MIGRATIONS_DIR = join(process.cwd(), "supabase", "migrations");
const ALL_MIGRATIONS = readdirSync(MIGRATIONS_DIR).filter((f) => f.endsWith(".sql")).sort();

const SESSION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const STALE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"; // uploading, 2 h old
const FRESH_ID = "aaaaaaab-aaaa-4aaa-8aaa-aaaaaaaaaaaa"; // uploading, 10 min old
const QUEUED_ID = "aaaabbbb-aaaa-4aaa-8aaa-aaaaaaaaaaaa"; // queued, 2 h old
const RENDER_ID = "aaabcccc-aaaa-4aaa-8aaa-aaaaaaaaaaaa"; // rendering, 2 h old

async function bootstrap(): Promise<PGlite> {
  const db = await PGlite.create();
  await db.exec(`
    create role anon noinherit nologin;
    create role service_role noinherit nologin bypassrls;
    grant usage on schema public to anon, service_role;
  `);
  await db.exec(`
    create schema if not exists storage;
    create table if not exists storage.buckets (
        id uuid primary key,
        name text not null,
        owner uuid,
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now(),
        public boolean not null default false,
        avif_conversion boolean,
        file_size_limit bigint,
        allowed_mime_types text[]
    );
    create table if not exists storage.objects (
        id uuid primary key,
        bucket_id uuid,
        name text,
        owner uuid
    );
    alter table storage.objects enable row level security;
  `);
  return db;
}

async function apply(db: PGlite, files: string[]): Promise<void> {
  for (const file of files) {
    await db.exec(readFileSync(join(MIGRATIONS_DIR, file), "utf-8"));
  }
}

async function q(db: PGlite, sql: string): Promise<Array<Record<string, unknown>>> {
  const res = await db.query(sql);
  return res?.rows as Array<Record<string, unknown>>;
}

function state(db: PGlite, id: string) {
  return q(db, `select status, last_error, locked_by, locked_at, lease_expires_at, expires_at
                from public.jobs where id = '${id}'`);
}

describe("TASK 017 — reap_stale_uploading_jobs on the real engine", () => {
  let db: PGlite;

  beforeAll(async () => {
    db = await bootstrap();
    await apply(db, ALL_MIGRATIONS);
    await q(db, `insert into public.processing_sessions (id, token_hash, expires_at)
                 values ('${SESSION_ID}', 'hash', now() + interval '1 hour')`);
    // Upload jobs need a source_path (jobs_source_present); the stale one
    // is 2 h old, the fresh one 10 min old (default reaper age: 1080 s).
    await q(db, `insert into public.jobs
                 (id, session_id, source_type, source_path, generation_mode, status,
                  progress, created_at)
                 values
                 ('${STALE_ID}', '${SESSION_ID}', 'upload', 'sources/${STALE_ID}/reserved',
                  'full_coverage', 'uploading', 0, now() - interval '2 hours'),
                 ('${FRESH_ID}', '${SESSION_ID}', 'upload', 'sources/${FRESH_ID}/reserved',
                  'full_coverage', 'uploading', 0, now() - interval '10 minutes')`);
    // Non-upload rows of the same age — the reaper must never touch them.
    // (jobs_lock_consistency: 'rendering' REQUIRES a live lock triple.)
    await q(db, `insert into public.jobs
                 (id, session_id, source_type, source_url, generation_mode, status,
                  progress, created_at, locked_by, locked_at, lease_expires_at)
                 values
                 ('${QUEUED_ID}', '${SESSION_ID}', 'direct_url', 'https://cdn.example.com/v.mp4',
                  'full_coverage', 'queued', 0, now() - interval '2 hours',
                  null, null, null),
                 ('${RENDER_ID}', '${SESSION_ID}', 'direct_url', 'https://cdn.example.com/v2.mp4',
                  'full_coverage', 'rendering', 40, now() - interval '2 hours',
                  'test-worker:1:abcd1234', now() - interval '2 hours', now() + interval '15 minutes')`);
  });
  afterAll(async () => {
    await db?.close();
  });

  it("applies to the 16-migration chain (15 pre-existing + the reaper)", async () => {
    expect(ALL_MIGRATIONS).toHaveLength(16);
    expect(ALL_MIGRATIONS.at(-1)).toBe("20261131000000_task17_stale_upload_reaper.sql");
  });

  it("fails only the STALE uploading row (returns the exact count)", async () => {
    const rows = await q(db, `select public.reap_stale_uploading_jobs(1080) as n`);
    expect(Number(rows[0]?.n)).toBe(1);
  });

  it("the reaped row is terminal, lock-free, explained, and retention-armed", async () => {
    const [row] = await state(db, STALE_ID);
    expect(row?.status).toBe("failed");
    expect(String(row?.last_error)).toMatch(/did not finish in time/i);
    expect(row?.locked_by).toBeNull();
    expect(row?.locked_at).toBeNull();
    expect(row?.lease_expires_at).toBeNull();
    // Phase 6 retention trigger: terminal transition armed expires_at
    // (~30 days out) — a partially uploaded object is now purgeable.
    expect(row?.expires_at).not.toBeNull();
    const deadline = new Date(String(row?.expires_at)).getTime();
    expect(deadline).toBeGreaterThan(Date.now() + 29 * 86_400_000);
    expect(deadline).toBeLessThan(Date.now() + 31 * 86_400_000);
  });

  it("leaves fresh uploads and other statuses untouched", async () => {
    const [fresh] = await state(db, FRESH_ID);
    expect(fresh?.status).toBe("uploading");
    const [queued] = await state(db, QUEUED_ID);
    expect(queued?.status).toBe("queued");
    const [render] = await state(db, RENDER_ID);
    expect(render?.status).toBe("rendering");
  });

  it("is idempotent: a second run reaps nothing", async () => {
    const rows = await q(db, `select public.reap_stale_uploading_jobs(1080) as n`);
    expect(Number(rows[0]?.n)).toBe(0);
  });

  it("validates the age threshold server-side (rejects < 60 s)", async () => {
    await expect(q(db, `select public.reap_stale_uploading_jobs(59)`)).rejects.toThrow(
      /at least 60 seconds/i
    );
  });

  it("reaps the fresh upload when the threshold is large enough (count 1)", async () => {
    // 10 min old < 1080 s (default) but > 600 s.
    const rows = await q(db, `select public.reap_stale_uploading_jobs(600) as n`);
    expect(Number(rows[0]?.n)).toBe(1);
    const [row] = await state(db, FRESH_ID);
    expect(row?.status).toBe("failed");
    expect(row?.expires_at).not.toBeNull();
  });

  it("is SECURITY DEFINER and grants EXECUTE to service_role but not anon", async () => {
    const func = await q(
      db,
      `select p.prosecdef
         from pg_proc p
         join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public' and p.proname = 'reap_stale_uploading_jobs'`
    );
    expect(func[0]?.prosecdef).toBe(true);

    // Authoritative check: try the call as each role (EXECUTE granted to
    // service_role, denied to anon — same trust model as the queue RPCs).
    await q(db, `set role service_role`);
    try {
      const asService = await q(db, `select public.reap_stale_uploading_jobs(1080) as n`);
      expect(Number(asService[0]?.n)).toBe(0); // nothing left — callable
    } finally {
      await q(db, `reset role`);
    }
    await q(db, `set role anon`);
    try {
      await expect(q(db, `select public.reap_stale_uploading_jobs(1080)`)).rejects.toThrow(
        /permission denied/i
      );
    } finally {
      await q(db, `reset role`);
    }
  });
});
