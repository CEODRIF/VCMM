import { beforeEach, describe, expect, it, vi } from "vitest";

// Mock the database layer so session logic is testable without Supabase.
vi.mock("@/lib/db/sessions", () => ({
  findSessionByTokenHash: vi.fn(),
  createSessionRow: vi.fn(),
  deleteSessionRow: vi.fn(),
}));

import { securityConfig } from "@/config/security";
import {
  constantTimeEqual,
  generateSessionToken,
  hashAccessCode,
  normalizeAccessCodeHash,
  sha256Hex,
} from "@/lib/security/crypto";
import {
  buildClearSessionCookie,
  buildSessionCookie,
  getSessionToken,
  parseCookieHeader,
  verifySessionToken,
} from "@/lib/security/session";
import { createSessionRow, deleteSessionRow, findSessionByTokenHash } from "@/lib/db/sessions";

const findMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const createMock = createSessionRow as ReturnType<typeof vi.fn>;
const deleteMock = deleteSessionRow as ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
});

describe("cryptographic primitives", () => {
  it("computes known SHA-256 vectors", () => {
    expect(sha256Hex("")).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    expect(sha256Hex("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });

  it("constantTimeEqual matches semantics of equality", () => {
    expect(constantTimeEqual("same-value", "same-value")).toBe(true);
    expect(constantTimeEqual("value-a", "value-b")).toBe(false);
    // Different lengths must not throw and must be unequal.
    expect(constantTimeEqual("short", "a-much-longer-value")).toBe(false);
  });

  it("generates 256-bit hex session tokens with high uniqueness", () => {
    const tokens = new Set<string>();
    for (let i = 0; i < 1000; i++) {
      const token = generateSessionToken(securityConfig.session.tokenBytes);
      expect(token).toMatch(/^[0-9a-f]{64}$/);
      tokens.add(token);
    }
    expect(tokens.size).toBe(1000);
  });

  it("hashAccessCode trims input", () => {
    expect(hashAccessCode("  my-code  ")).toBe(sha256Hex("my-code"));
  });

  it("normalizes stored access-code hash formats", () => {
    expect(normalizeAccessCodeHash("sha256:ABCDEF")).toBe("abcdef");
    expect(normalizeAccessCodeHash("ABCDEF")).toBe("abcdef");
    expect(normalizeAccessCodeHash("  sha256:ABCDEF  ")).toBe("abcdef");
  });
});

describe("session cookie handling", () => {
  it("builds an HttpOnly, SameSite=Lax cookie with the TTL", () => {
    const cookie = buildSessionCookie("tok123", 3600, false);
    expect(cookie).toContain("clipforge_session=tok123");
    expect(cookie).toContain("HttpOnly");
    expect(cookie).toContain("SameSite=Lax");
    expect(cookie).toContain("Path=/");
    expect(cookie).toContain("Max-Age=3600");
    expect(cookie).not.toContain("Secure");
  });

  it("adds the Secure flag only in production", () => {
    expect(buildSessionCookie("tok123", 3600, true)).toContain("Secure");
  });

  it("clears the cookie with Max-Age=0", () => {
    const cookie = buildClearSessionCookie();
    expect(cookie).toContain("Max-Age=0");
  });

  it("parses raw Cookie headers", () => {
    expect(parseCookieHeader("a=b; c=d")).toEqual({ a: "b", c: "d" });
    expect(parseCookieHeader(null)).toEqual({});
    expect(parseCookieHeader("  spaced = value ; x=1  ")).toEqual({ spaced: "value", x: "1" });
  });

  it("extracts the session token from a Request", () => {
    const request = new Request("http://localhost/api/v1/jobs", {
      headers: { cookie: "other=1; clipforge_session=raw-token" },
    });
    expect(getSessionToken(request)).toBe("raw-token");
    expect(getSessionToken(new Request("http://localhost/x"))).toBeUndefined();
  });
});

describe("session verification", () => {
  it("rejects missing tokens without touching the database", async () => {
    expect(await verifySessionToken(undefined)).toBeNull();
    expect(await verifySessionToken("")).toBeNull();
    expect(findMock).not.toHaveBeenCalled();
  });

  it("rejects unknown tokens (hash lookup miss)", async () => {
    findMock.mockResolvedValue(null);
    expect(await verifySessionToken("unknown-token")).toBeNull();
  });

  it("rejects expired sessions and cleans them up", async () => {
    const past = new Date(Date.now() - 1000).toISOString();
    findMock.mockResolvedValue({ id: "sess-1", expires_at: past });
    deleteMock.mockResolvedValue(undefined);
    expect(await verifySessionToken("expired-token")).toBeNull();
    expect(deleteMock).toHaveBeenCalledWith("sess-1");
  });

  it("accepts valid sessions and returns their id", async () => {
    const future = new Date(Date.now() + 60_000).toISOString();
    findMock.mockResolvedValue({ id: "sess-2", expires_at: future });
    const session = await verifySessionToken("valid-token");
    expect(session).toEqual({ sessionId: "sess-2", expiresAt: future });
    // The database is queried by the SHA-256 hash of the raw token.
    expect(findMock).toHaveBeenCalledWith(sha256Hex("valid-token"));
  });
});
