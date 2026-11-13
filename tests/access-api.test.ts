import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the database layer; the route logic under test stays real.
vi.mock("@/lib/db/sessions", () => ({
  findSessionByTokenHash: vi.fn(),
  createSessionRow: vi.fn(),
  deleteSessionRow: vi.fn(),
}));

import { POST, DELETE } from "@/app/api/access/route";
import { createSessionRow, deleteSessionRow, findSessionByTokenHash } from "@/lib/db/sessions";
import { accessCodeRateLimiter } from "@/lib/security/rate-limit";
import { sha256Hex } from "@/lib/security/crypto";

const findMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const createMock = createSessionRow as ReturnType<typeof vi.fn>;
const deleteMock = deleteSessionRow as ReturnType<typeof vi.fn>;

const ACCESS_CODE = "correct-horse-battery";
const ACCESS_CODE_HASH = sha256Hex(ACCESS_CODE);
const FUTURE = new Date(Date.now() + 3600_000).toISOString();

function postRequest(body: unknown, cookie?: string): Request {
  return new Request("http://localhost/api/access", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(cookie ? { cookie } : {}),
    },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  accessCodeRateLimiter.resetAll();
  process.env.ACCESS_CODE_HASH = ACCESS_CODE_HASH;
  createMock.mockResolvedValue("session-row-id");
  findMock.mockResolvedValue(null);
});

afterEach(() => {
  delete process.env.ACCESS_CODE_HASH;
});

describe("POST /api/access — authentication", () => {
  it("rejects a wrong code with 401 and creates no session", async () => {
    const response = await POST(postRequest({ code: "wrong-code" }));
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "Invalid access code." });
    expect(createMock).not.toHaveBeenCalled();
  });

  it("rejects a missing code with 400", async () => {
    const response = await POST(postRequest({}));
    expect(response.status).toBe(400);
  });

  it("rejects malformed JSON with 400", async () => {
    const response = await POST(postRequest("this is not json"));
    expect(response.status).toBe(400);
  });

  it("returns 503 when the service is not configured", async () => {
    delete process.env.ACCESS_CODE_HASH;
    const response = await POST(postRequest({ code: ACCESS_CODE }));
    expect(response.status).toBe(503);
    expect(createMock).not.toHaveBeenCalled();
  });

  it("accepts the correct code and issues an HttpOnly session cookie", async () => {
    const response = await POST(postRequest({ code: ACCESS_CODE }));
    expect(response.status).toBe(200);

    const body = (await response.json()) as { ok: boolean; expiresInHours: number };
    expect(body.ok).toBe(true);
    expect(body.expiresInHours).toBe(24);

    const setCookie = response.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie).toContain("SameSite=Lax");
    expect(setCookie).toContain("Max-Age=86400");

    // The cookie carries a raw 256-bit token; only its hash reaches the DB.
    const token = setCookie.split(";")[0]?.split("=")[1] ?? "";
    expect(token).toMatch(/^[0-9a-f]{64}$/);

    expect(createMock).toHaveBeenCalledTimes(1);
    const [storedHash, expiresAt] = createMock.mock.calls[0] as unknown as [string, string];
    expect(storedHash).toBe(sha256Hex(token));
    expect(storedHash).not.toBe(token);
    // Expiry is ~24h in the future.
    const expiry = new Date(expiresAt).getTime();
    expect(expiry).toBeGreaterThan(Date.now() + 23 * 3600_000);
    expect(expiry).toBeLessThanOrEqual(Date.now() + 24 * 3600_000 + 1000);
  });

  it("accepts the documented sha256:<hex> hash format", async () => {
    process.env.ACCESS_CODE_HASH = `sha256:${ACCESS_CODE_HASH}`;
    const response = await POST(postRequest({ code: ACCESS_CODE }));
    expect(response.status).toBe(200);
  });

  it("rate-limits repeated attempts with 429 after 5 tries", async () => {
    for (let i = 0; i < 5; i++) {
      const response = await POST(postRequest({ code: "wrong" }));
      expect(response.status).toBe(401);
    }
    const blocked = await POST(postRequest({ code: ACCESS_CODE }));
    expect(blocked.status).toBe(429);
    const body = (await blocked.json()) as { error: string };
    expect(body.error).toMatch(/too many attempts/i);
    expect(createMock).not.toHaveBeenCalled();
  });
});

describe("DELETE /api/access — logout", () => {
  it("destroys a known session and clears the cookie", async () => {
    const token = "a".repeat(64);
    findMock.mockResolvedValue({ id: "sess-x", expires_at: FUTURE });
    deleteMock.mockResolvedValue(undefined);

    const response = await DELETE(
      new Request("http://localhost/api/access", {
        method: "DELETE",
        headers: { cookie: `clipforge_session=${token}` },
      })
    );
    expect(response.status).toBe(200);
    expect(deleteMock).toHaveBeenCalledWith("sess-x");
    expect(response.headers.get("set-cookie")).toContain("Max-Age=0");
  });

  it("is idempotent without a cookie", async () => {
    const response = await DELETE(new Request("http://localhost/api/access", { method: "DELETE" }));
    expect(response.status).toBe(200);
    expect(deleteMock).not.toHaveBeenCalled();
  });
});
