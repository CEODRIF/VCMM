import { describe, expect, it } from "vitest";

import { parseClientEnv, parseServerEnv } from "@/lib/env";

describe("environment variable validation", () => {
  it("accepts a fully configured server environment", () => {
    const result = parseServerEnv({
      NODE_ENV: "production",
      SUPABASE_URL: "https://xyz.supabase.co",
      SUPABASE_SERVICE_ROLE_KEY: "placeholder-service-role-key",
      ACCESS_CODE_HASH: "sha256:abc123",
      DATABASE_URL: "postgres://user:pass@localhost:5432/clipforge",
    });
    expect(result.success).toBe(true);
    if (result.success && result.data) {
      expect(result.data.NODE_ENV).toBe("production");
      expect(result.data.SUPABASE_URL).toBe("https://xyz.supabase.co");
    }
  });

  it("allows an unconfigured development environment (defaults apply)", () => {
    const result = parseServerEnv({});
    expect(result.success).toBe(true);
    if (result.success && result.data) {
      expect(result.data.NODE_ENV).toBe("development");
    }
  });

  it("rejects an invalid SUPABASE_URL", () => {
    const result = parseServerEnv({ SUPABASE_URL: "not-a-url" });
    expect(result.success).toBe(false);
    expect(result.errors?.some((entry) => entry.includes("SUPABASE_URL"))).toBe(true);
  });

  it("rejects an empty DATABASE_URL string", () => {
    const result = parseServerEnv({ DATABASE_URL: "" });
    expect(result.success).toBe(false);
    expect(result.errors?.some((entry) => entry.includes("DATABASE_URL"))).toBe(true);
  });

  it("accepts an empty client environment", () => {
    expect(parseClientEnv({}).success).toBe(true);
  });

  it("accepts a valid public client environment", () => {
    const result = parseClientEnv({
      NEXT_PUBLIC_APP_URL: "http://localhost:3000",
      NEXT_PUBLIC_SUPABASE_URL: "https://xyz.supabase.co",
      NEXT_PUBLIC_SUPABASE_ANON_KEY: "placeholder-anon-key",
    });
    expect(result.success).toBe(true);
  });

  it("rejects a public client environment with a non-URL app URL", () => {
    const result = parseClientEnv({ NEXT_PUBLIC_APP_URL: "localhost:3000" });
    expect(result.success).toBe(false);
    expect(result.errors?.some((entry) => entry.includes("NEXT_PUBLIC_APP_URL"))).toBe(true);
  });

  it("rejects a public client environment with an empty anon key", () => {
    const result = parseClientEnv({ NEXT_PUBLIC_SUPABASE_ANON_KEY: "" });
    expect(result.success).toBe(false);
  });
});
