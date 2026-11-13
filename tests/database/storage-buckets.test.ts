/**
 * Phase 3 — storage bucket migration test against a REAL PostgreSQL engine.
 *
 * The migration (20261115000001) is Supabase-specific (it references the
 * `storage` schema). To execute it for real — not just parse it — this test
 * builds a minimal structural stub of Supabase's `storage.buckets` /
 * `storage.objects` tables (same column types as Supabase's schema,
 * including the uuid bucket id) and applies the migration file on top.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { PGlite } from "@electric-sql/pglite";

type Db = InstanceType<typeof PGlite>;
type Row = Record<string, unknown>;

let db: Db;

async function rows(sql: string): Promise<Row[]> {
  const result = await db.query(sql);
  return result.rows as Row[];
}

async function singleValue(sql: string): Promise<unknown> {
  const result = await db.query(sql);
  const row = (result.rows as Row[])[0];
  return row ? Object.values(row)[0] : null;
}

beforeAll(async () => {
  db = await PGlite.create();

  // Structural stub of Supabase's storage schema (column types mirror the
  // real tables so the migration's uuid ids / conflict handling behave).
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

  const migration = readFileSync(
    join(process.cwd(), "supabase", "migrations", "20261115000001_phase3_storage_buckets.sql"),
    "utf-8"
  );
  await db.exec(migration);
});

afterAll(async () => {
  await db?.close();
});

describe("storage bucket configuration", () => {
  it("creates both private buckets", async () => {
    const buckets = await rows(
      "select name, public from storage.buckets order by name"
    );
    expect(buckets).toHaveLength(2);
    for (const bucket of buckets) {
      expect(bucket.public).toBe(false);
    }
    const names = buckets.map((b) => b.name as string);
    expect(names).toContain("clipforge-sources");
    expect(names).toContain("clipforge-clips");
  });

  it("is idempotent (re-running the migration keeps exactly two private buckets)", async () => {
    const migration = readFileSync(
      join(process.cwd(), "supabase", "migrations", "20261115000001_phase3_storage_buckets.sql"),
      "utf-8"
    );
    await db.exec(migration);
    await db.exec(migration);

    const count = (await singleValue("select count(*)::int from storage.buckets")) as number;
    expect(count).toBe(2);
    const publicCount = (
      await singleValue("select count(*)::int from storage.buckets where public = true")
    ) as number;
    expect(publicCount).toBe(0);
  });

  it("forces private=true back to false if a bucket row was ever flipped public", async () => {
    await db.query("update storage.buckets set public = true where name = 'clipforge-sources'");
    const migration = readFileSync(
      join(process.cwd(), "supabase", "migrations", "20261115000001_phase3_storage_buckets.sql"),
      "utf-8"
    );
    await db.exec(migration);
    const isPublic = (
      await singleValue("select public from storage.buckets where name = 'clipforge-sources'")
    ) as boolean;
    expect(isPublic).toBe(false);
  });

  it("drops any accidental public-read storage policy (defense in depth)", async () => {
    await db.query(
      `create policy public_read_sources on storage.objects for select using (bucket_id = (select id from storage.buckets where name = 'clipforge-sources'))`
    );
    const migration = readFileSync(
      join(process.cwd(), "supabase", "migrations", "20261115000001_phase3_storage_buckets.sql"),
      "utf-8"
    );
    await db.exec(migration);

    const policies = await rows(
      "select policyname from pg_policies where tablename = 'objects' and schemaname = 'storage' and policyname in ('public_read_sources', 'public_read_clips')"
    );
    expect(policies).toHaveLength(0);
  });

  it("creates no storage policies for the buckets (deny-by-default client access)", async () => {
    const policies = await rows(
      "select count(*)::int as n from pg_policies where schemaname = 'storage'"
    );
    expect(policies[0]?.n).toBe(0);
  });
});
