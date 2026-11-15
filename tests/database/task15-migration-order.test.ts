import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { PGlite } from "@electric-sql/pglite";

/**
 * TASK 015 P5 — migration ordering proof on a REAL Postgres engine (PGlite).
 *
 * Proves (no static inspection, no claims without execution):
 *   1. CLEAN INSTALL: every migration in supabase/migrations/ applies in
 *      filename order to an empty database, and the final schema has the
 *      TASK 014 + TASK 015 contract (source types incl. kick, analysis
 *      timeline, generation modes/FPS, measured clip metadata, 'deleting'
 *      state, cleanup tracking, private storage buckets).
 *   2. UPGRADE PATH: a database in the exact end-state of TASK 014
 *      (migrations 1..11) upgrades cleanly by applying ONLY the four
 *      TASK 015 migrations (12..15), reaching the same final contract —
 *      and re-applying them is a no-op (idempotent).
 *   3. Source-type + generation-mode constraint compatibility probes.
 *
 * Mirrors the Supabase role model (anon/service_role) and stubs the
 * storage schema exactly like storage-buckets.test.ts.
 */

const MIGRATIONS_DIR = join(process.cwd(), "supabase", "migrations");
const ALL_MIGRATIONS = readdirSync(MIGRATIONS_DIR).filter((f) => f.endsWith(".sql")).sort();
// The four TASK 015 migrations, in dependency order.
const TASK15_MIGRATIONS = [
  "20261127000000_task15_kick_source.sql",
  "20261128000000_task15_analysis_timeline.sql",
  "20261129000000_task15_generation_modes.sql",
  "20261130000000_task15_results_actions.sql",
];
// Everything up to and including TASK 014 (the P4 base state).
const PRE_TASK15_MIGRATIONS = ALL_MIGRATIONS.filter((f) => !TASK15_MIGRATIONS.includes(f));

const JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const SESSION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

async function bootstrap(): Promise<PGlite> {
  const db = await PGlite.create();
  await db.exec(`
    create role anon noinherit nologin;
    create role service_role noinherit nologin bypassrls;
    grant usage on schema public to anon, service_role;
  `);
  // Structural stub of Supabase's storage schema (mirrors storage-buckets.test.ts).
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

function constraintDef(rows: Array<Record<string, unknown>>): string {
  return rows.map((r) => String(r.condef)).join("\n");
}

async function finalContract(db: PGlite): Promise<void> {
  // Source types: TASK 014 (direct_url) + TASK 015 P1 (kick).
  const sourceCheck = constraintDef(
    await q(db, "select pg_get_constraintdef(oid) as condef from pg_constraint where conname = 'jobs_source_type_check'")
  );
  for (const t of ["'youtube'", "'upload'", "'twitch'", "'direct_url'", "'kick'"]) {
    expect(sourceCheck).toContain(t);
  }

  // TASK 015 P2: the analysis timeline table exists.
  const tables = await q(db, "select table_name from information_schema.tables where table_schema = 'public'");
  expect(tables.map((t) => String(t.table_name)).sort()).toContain("analysis_timeline");

  // TASK 015 P3: generation mode + FPS + requested clip count.
  const cols = new Set(
    (await q(db, "select column_name from information_schema.columns where table_schema = 'public' and table_name = 'jobs'")).map(
      (c) => String(c.column_name)
    )
  );
  for (const c of ["generation_mode", "output_fps", "requested_clips"]) expect(cols.has(c)).toBe(true);
  // PG rewrites `in (30, 60)` as `= ANY (ARRAY[30, 60])` in the definition.
  const fpsCheck = constraintDef(
    await q(db, "select pg_get_constraintdef(oid) as condef from pg_constraint where conname = 'jobs_output_fps_check'")
  );
  expect(fpsCheck).toContain("ARRAY[30, 60]");

  // TASK 015 P4: measured clip metadata, 'deleting' state, cleanup tracking.
  const clipCols = new Set(
    (await q(db, "select column_name from information_schema.columns where table_schema = 'public' and table_name = 'clips'")).map(
      (c) => String(c.column_name)
    )
  );
  for (const c of ["width", "height", "output_fps", "file_size_bytes", "mode"]) expect(clipCols.has(c)).toBe(true);
  const statusCheck = constraintDef(
    await q(db, "select pg_get_constraintdef(oid) as condef from pg_constraint where conname = 'clips_status_check'")
  );
  expect(statusCheck).toContain("'deleting'");
  const jobCols = new Set(
    (await q(db, "select column_name from information_schema.columns where table_schema = 'public' and table_name = 'jobs'")).map(
      (c) => String(c.column_name)
    )
  );
  for (const c of ["cleanup_error", "purged_at", "expires_at"]) expect(jobCols.has(c)).toBe(true);

  // RLS remains enabled on the core tables (ownership model intact).
  // The design is DENY-BY-DEFAULT: RLS on + no client-facing policies,
  // only service_role (BYPASSRLS) reads the data.
  for (const table of ["jobs", "clips", "processing_sessions"]) {
    const rls = await q(db, `select relrowsecurity from pg_class where relname = '${table}'`);
    expect(rls[0]?.relrowsecurity).toBe(true);
  }

  // Private buckets present (Phase 3 storage migration).
  const buckets = await q(db, "select name, public from storage.buckets order by name");
  expect(buckets.map((b) => `${b.name}:${b.public === true ? "public" : "private"}`)).toEqual([
    "clipforge-clips:private",
    "clipforge-sources:private",
  ]);
}

interface JobOverrides {
  type: string;
  source: string;
  sourceValue: string;
  mode: string;
  fpsValue: string;
  count: string;
  countValue: string;
}

async function insertJob(db: PGlite, overrides: JobOverrides): Promise<void> {
  // jobs.session_id is an FK to processing_sessions — the session must exist.
  await q(db, `insert into public.processing_sessions (id, token_hash, expires_at)
             values ('${SESSION_ID}', 'hash', now() + interval '1 hour')
             on conflict (id) do nothing`);
  await q(db, `insert into public.jobs
           (id, session_id, source_type, ${overrides.source}, ${overrides.count},
            clip_duration_seconds, status, generation_mode, output_fps, progress, completed_at)
           values ('${JOB_ID}', '${SESSION_ID}', '${overrides.type}',
                   ${overrides.sourceValue}, ${overrides.countValue},
                   60, 'completed', '${overrides.mode}', ${overrides.fpsValue}, 100, now())`);
}

describe("TASK 015 migration order: clean install", () => {
  let db: PGlite;

  beforeAll(async () => {
    db = await bootstrap();
    await apply(db, ALL_MIGRATIONS);
  });
  afterAll(async () => {
    await db?.close();
  });

  it("applies ALL migrations in order to an empty database", async () => {
    // 16 = 11 pre-TASK-015 + 4 TASK 015 + 1 TASK 017 (stale-upload reaper).
    expect(ALL_MIGRATIONS).toHaveLength(16);
    expect(ALL_MIGRATIONS.at(-1)).toBe("20261131000000_task17_stale_upload_reaper.sql");
    expect(TASK15_MIGRATIONS).toEqual(ALL_MIGRATIONS.slice(-5, -1));
    // The reaper RPC is part of the final contract.
    const funcs = await db.query(
      "select count(*)::int as n from pg_proc p join pg_namespace ns on ns.oid = p.pronamespace where ns.nspname = 'public' and p.proname = 'reap_stale_uploading_jobs'"
    );
    expect(Number((funcs.rows[0] as { n: number }).n)).toBe(1);
  });

  it("reaches the TASK 014 + TASK 015 final contract", async () => {
    await finalContract(db);
  });

  it("accepts every supported source type with its required field (rejects missing)", async () => {
    const good: Array<[string, string, string]> = [
      ["youtube", "source_url", "'https://youtube.com/watch?v=abc'"],
      ["upload", "source_path", `'sources/${JOB_ID}/original/v.mp4'`],
      ["twitch", "source_url", "'https://www.twitch.tv/videos/123'"],
      ["kick", "source_url", "'https://kick.com/channel/x'"],
      ["direct_url", "source_url", "'https://cdn.example.com/v.mp4'"],
    ];
    for (const [type, col, value] of good) {
      await q(db, `delete from public.jobs`);
      // full_coverage + NULL count satisfies jobs_mode_count_consistency.
      await insertJob(db, {
        type,
        source: col,
        sourceValue: value,
        mode: "full_coverage",
        fpsValue: "null",
        count: "requested_clips",
        countValue: "null",
      });
    }
    // An upload job WITHOUT source_path must be rejected.
    await q(db, `delete from public.jobs`);
    await expect(
      q(db, `insert into public.jobs (id, session_id, source_type, status, generation_mode, progress, completed_at)
             values ('${JOB_ID}', '${SESSION_ID}', 'upload', 'completed', 'full_coverage', 100, now())`)
    ).rejects.toThrow(); // full_coverage+NULL count is mode-legal, so source_present is what rejects
  });

  it("grants the anon role nothing at the DB level on the final schema", async () => {
    await q(db, `delete from public.jobs`);
    await insertJob(db, {
      type: "upload",
      source: "source_path",
      sourceValue: `'sources/${JOB_ID}/original/v.mp4'`,
      mode: "full_coverage",
      fpsValue: "null",
      count: "requested_clips",
      countValue: "null",
    });
    // The migrations grant anon NO table privileges (only service_role gets
    // RPC EXECUTE); if a deployment later grants table SELECT, the
    // RLS-enabled, policy-free tables (checked in finalContract) still deny.
    await q(db, `set role anon`);
    try {
      await expect(q(db, "select count(*)::int as n from public.jobs")).rejects.toThrow(
        /permission denied/
      );
    } finally {
      await q(db, `reset role`);
    }
    const visible = await q(db, "select count(*)::int as n from public.jobs");
    expect(Number(visible[0]?.n)).toBe(1); // the server role sees the row
  });

  it("enforces the NULL-safe mode/clip-count invariant", async () => {
    await q(db, `delete from public.jobs`);
    // highlights REQUIRES requested_clips (1..10) — NULL must be rejected.
    await expect(
      q(db, `insert into public.jobs (id, session_id, source_type, source_path, generation_mode, status, progress, completed_at)
             values ('${JOB_ID}', '${SESSION_ID}', 'upload', 'sources/${JOB_ID}/original/v.mp4',
                     'highlights', 'completed', 100, now())`)
    ).rejects.toThrow();
    // full_coverage REQUIRES requested_clips to be NULL.
    await expect(
      q(db, `insert into public.jobs (id, session_id, source_type, source_path, generation_mode, requested_clips, status, progress, completed_at)
             values ('${JOB_ID}', '${SESSION_ID}', 'upload', 'sources/${JOB_ID}/original/v.mp4',
                     'full_coverage', 12, 'completed', 100, now())`)
    ).rejects.toThrow();
    // highlights with a valid count (1..10) is the accepted combination.
    await insertJob(db, {
      type: "upload",
      source: "source_path",
      sourceValue: `'sources/${JOB_ID}/original/v.mp4'`,
      mode: "highlights",
      fpsValue: "60",
      count: "requested_clips",
      countValue: "3",
    });
  });
});

describe("TASK 015 migration order: upgrade from the TASK 014 end-state", () => {
  let db: PGlite;

  beforeAll(async () => {
    db = await bootstrap();
    // The exact state a TASK 014 deployment has.
    await apply(db, PRE_TASK15_MIGRATIONS);
  });
  afterAll(async () => {
    await db?.close();
  });

  it("the pre-015 state genuinely lacks the TASK 015 contract", async () => {
    const tables = await q(db, "select table_name from information_schema.tables where table_schema = 'public'");
    expect(tables.map((t) => String(t.table_name))).not.toContain("analysis_timeline");
    const sourceCheck = constraintDef(
      await q(db, "select pg_get_constraintdef(oid) as condef from pg_constraint where conname = 'jobs_source_type_check'")
    );
    expect(sourceCheck).toContain("'direct_url'");
    expect(sourceCheck).not.toContain("'kick'");
    const jobCols = await q(db, "select column_name from information_schema.columns where table_schema = 'public' and table_name = 'jobs'");
    expect(jobCols.map((c) => String(c.column_name))).not.toContain("cleanup_error");
  });

  it("the four TASK 015 migrations upgrade it to the final contract", async () => {
    await apply(db, TASK15_MIGRATIONS);
    await finalContract(db);
  });

  it("re-applying the four TASK 015 migrations is a no-op (idempotent)", async () => {
    await q(db, `delete from public.jobs`);
    await insertJob(db, {
      type: "upload",
      source: "source_path",
      sourceValue: `'sources/${JOB_ID}/original/v.mp4'`,
      mode: "full_coverage",
      fpsValue: "null",
      count: "requested_clips",
      countValue: "null",
    });
    await q(db, `insert into public.clips
             (id, job_id, title, start_time, end_time, status, width, height, output_fps)
             values ('cccccccc-cccc-4ccc-8ccc-cccccccccccc', '${JOB_ID}', 'c', 32, 92, 'deleting', 1080, 1920, 60)`);
    await apply(db, TASK15_MIGRATIONS);
    const clip = (await q(db, "select status, width from public.clips limit 1"))[0]!;
    expect(clip.status).toBe("deleting");
    expect(Number(clip.width)).toBe(1080);
  });
});
