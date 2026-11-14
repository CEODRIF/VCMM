/**
 * TASK 014 — universal link ingestion schema test against a REAL
 * PostgreSQL engine (PGlite WASM).
 *
 * Verifies the repository-only migration `20261126000000`:
 *   - `jobs.source_type` accepts 'direct_url' WITH source_url (a public
 *     http(s) video file link — the worker imports it through the
 *     SSRF-guarded streaming path)
 *   - 'direct_url' WITHOUT a URL is rejected by jobs_source_present
 *   - the pre-existing youtube/upload/twitch branches are UNCHANGED
 *     (regression: each still requires its own source column)
 *   - an unknown source type is still rejected
 *   - re-applying the migration is a no-op (idempotent)
 *
 * NOTE: this migration ships for MANUAL deployment only (see the file
 * header) — nothing in the test suite or CI applies it to a live
 * database; PGlite here validates the delta in isolation.
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

let seq = 0;

/**
 * Seeds a session + a job of the given shape, returns its id.
 * `source` is "url" (a public file link), "path" (a storage object),
 * or null (NEITHER — for constraint-rejection cases).
 */
async function seedJob(sourceType: string, source: "url" | "path" | null): Promise<string> {
  seq += 1;
  const sessionId = `11111111-1111-4111-8111-${String(140000000000 + seq).padStart(12, "0")}`;
  const jobId = `22222222-2222-4222-8222-${String(140000000000 + seq).padStart(12, "0")}`;
  await db.query(
    `insert into public.processing_sessions (id, token_hash, expires_at) values ('${sessionId}', 't14-${seq}', now() + interval '1 hour')`
  );
  const sourceCol = source === "path" ? "source_path" : "source_url";
  const sourceVal =
    source === "url"
      ? "'https://storage.example.com/streams/vod.mp4'"
      : source === "path"
        ? "'sources/x/original/v.mp4'"
        : "null";
  await db.query(
    `insert into public.jobs (id, session_id, source_type, ${sourceCol}, requested_clips, clip_duration_seconds, status)
     values ('${jobId}', '${sessionId}', '${sourceType}', ${sourceVal}, 1, 60, 'queued')`
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
  "20261125000000_phase7b_intel_integration.sql",
  "20261126000000_task14_universal_link_source.sql",
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

describe("TASK 014 migration: direct_url source type", () => {
  it("accepts a direct_url job carrying its link in source_url", async () => {
    const jobId = await seedJob("direct_url", "url");
    const rows = await q(`select source_type, source_url from public.jobs where id = '${jobId}'`);
    expect(rows).toHaveLength(1);
    const row = rows[0] as Row;
    expect(row.source_type).toBe("direct_url");
    expect(row.source_url).toBe("https://storage.example.com/streams/vod.mp4");
  });

  it("rejects a direct_url job without source_url (jobs_source_present)", async () => {
    await expect(seedJob("direct_url", null)).rejects.toThrow();
  });

  it("leaves the pre-existing source types unchanged (regression)", async () => {
    // youtube: still requires source_url.
    await seedJob("youtube", "url");
    await expect(seedJob("youtube", "path")).rejects.toThrow();
    // upload: still requires source_path.
    await seedJob("upload", "path");
    await expect(seedJob("upload", "url")).rejects.toThrow();
    // twitch: still requires source_url.
    await seedJob("twitch", "url");
    await expect(seedJob("twitch", "path")).rejects.toThrow();
  });

  it("still rejects unknown source types", async () => {
    await expect(seedJob("vimeo", "url")).rejects.toThrow();
  });

  it("is idempotent (re-applying is a no-op)", async () => {
    const sql = readFileSync(
      join(MIGRATIONS_DIR, "20261126000000_task14_universal_link_source.sql"),
      "utf-8"
    );
    await db.exec(sql);
    const jobId = await seedJob("direct_url", "url");
    expect(jobId).toBeTruthy();
  });
});
