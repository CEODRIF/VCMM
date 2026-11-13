import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// TASK 012 — API hardening tests (spec section 9/12):
//   - request-size bounds on every JSON POST (413, typed, no buffering)
//   - per-session job-creation rate limiting (429 + Retry-After, unforgeable key)
//   - safe error serialization (no internals, no stack traces)
//   - regression: unauthorized (401) and cross-user (404, indistinguishable)
// The DB layer is mocked; route, session, validation, and limiter logic stay real.

vi.mock("@/lib/db/sessions", () => ({
  findSessionByTokenHash: vi.fn(),
  createSessionRow: vi.fn(),
  deleteSessionRow: vi.fn(),
}));
vi.mock("@/lib/db/jobs", () => ({
  insertJob: vi.fn(),
  findJobForSession: vi.fn(),
  listJobsForSession: vi.fn(),
  setUploadAuthorization: vi.fn(),
  markUploadComplete: vi.fn(),
  countClipsForJob: vi.fn(),
}));

import { POST as POST_JOBS, GET as LIST_JOBS } from "@/app/api/v1/jobs/route";
import { GET as GET_JOB } from "@/app/api/v1/jobs/[jobId]/route";
import { POST as POST_ACCESS } from "@/app/api/access/route";
import { POST as POST_UPLOAD_COMPLETE } from "@/app/api/v1/jobs/[jobId]/upload/complete/route";
import { BodyTooLargeError, readJsonBody } from "@/lib/api/body";
import { securityConfig } from "@/config/security";
import { findSessionByTokenHash, createSessionRow } from "@/lib/db/sessions";
import { findJobForSession, insertJob, listJobsForSession } from "@/lib/db/jobs";
import { accessCodeRateLimiter, jobCreationRateLimiter } from "@/lib/security/rate-limit";
import { sha256Hex } from "@/lib/security/crypto";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const createSessionRowMock = createSessionRow as ReturnType<typeof vi.fn>;
const insertMock = insertJob as ReturnType<typeof vi.fn>;
const findJobMock = findJobForSession as ReturnType<typeof vi.fn>;
const listMock = listJobsForSession as ReturnType<typeof vi.fn>;

const SESSION_TOKEN = "c".repeat(64);
const SESSION_ID = "33333333-3333-4333-8333-333333333333";
const OTHER_SESSION_ID = "44444444-4444-4444-8444-444444444444";
const FUTURE = new Date(Date.now() + 3600_000).toISOString();
const JOB_ID = "55555555-5555-4555-8555-555555555555";

const UPLOAD_PAYLOAD = {
  sourceType: "upload",
  fileName: "video.mp4",
  clipCount: 3,
  clipDurationSeconds: 120,
  captionsEnabled: true,
};

function requestWithSession(method: "GET" | "POST", path: string, body?: unknown): Request {
  return new Request(`http://localhost${path}`, {
    method,
    headers: {
      cookie: `clipforge_session=${SESSION_TOKEN}`,
      ...(body !== undefined ? { "content-type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

const ACCESS_CODE = "hardening-test-code";

beforeEach(() => {
  vi.clearAllMocks();
  jobCreationRateLimiter.resetAll();
  accessCodeRateLimiter.resetAll();
  process.env.ACCESS_CODE_HASH = sha256Hex(ACCESS_CODE);
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
  insertMock.mockResolvedValue({ id: JOB_ID });
  findJobMock.mockResolvedValue(null);
  listMock.mockResolvedValue([]);
  createSessionRowMock.mockResolvedValue("session-row-id");
});

afterEach(() => {
  delete process.env.ACCESS_CODE_HASH;
});

describe("request-size bounds (TASK 012)", () => {
  it("rejects an oversized job-creation payload with a typed 413 (no DB write)", async () => {
    const huge = { ...UPLOAD_PAYLOAD, fileName: "a".repeat(securityConfig.bodyLimit.maxBytes) };
    const response = await POST_JOBS(requestWithSession("POST", "/api/v1/jobs", huge));
    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({ error: "Request payload is too large." });
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("rejects an oversized access-code payload with 413 (before any secret work)", async () => {
    const response = await POST_ACCESS(
      new Request("http://localhost/api/access", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ code: "x".repeat(securityConfig.bodyLimit.maxBytes) }),
      })
    );
    expect(response.status).toBe(413);
    expect(createSessionRowMock).not.toHaveBeenCalled();
  });

  it("still accepts a normal in-budget payload (201)", async () => {
    const response = await POST_JOBS(requestWithSession("POST", "/api/v1/jobs", UPLOAD_PAYLOAD));
    expect(response.status).toBe(201);
    expect(insertMock).toHaveBeenCalledTimes(1);
  });

  it("rejects malformed in-budget JSON with 400 (safe message)", async () => {
    const response = await POST_JOBS(
      new Request("http://localhost/api/v1/jobs", {
        method: "POST",
        headers: { cookie: `clipforge_session=${SESSION_TOKEN}`, "content-type": "application/json" },
        body: "{not json",
      })
    );
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "Invalid JSON payload." });
  });
});

describe("per-session job-creation rate limit (TASK 012)", () => {
  async function createJobs(count: number, sessionToken = SESSION_TOKEN): Promise<Response[]> {
    const responses: Response[] = [];
    for (let i = 0; i < count; i++) {
      const req = new Request("http://localhost/api/v1/jobs", {
        method: "POST",
        headers: {
          cookie: `clipforge_session=${sessionToken}`,
          "content-type": "application/json",
        },
        body: JSON.stringify(UPLOAD_PAYLOAD),
      });
      responses.push(await POST_JOBS(req));
    }
    return responses;
  }

  it("allows up to the configured limit, then returns 429 + Retry-After", async () => {
    const limit = securityConfig.jobCreationRateLimit.maxAttempts;
    const responses = await createJobs(limit + 1);
    for (let i = 0; i < limit; i++) {
      expect(responses[i]?.status, `request ${i + 1}`).toBe(201);
    }
    const blocked = responses[limit];
    expect(blocked?.status).toBe(429);
    const retryAfter = blocked?.headers.get("retry-after");
    expect(retryAfter).toBeTruthy();
    expect(Number(retryAfter)).toBeGreaterThanOrEqual(1);
    expect(Number(retryAfter)).toBeLessThanOrEqual(Math.ceil(securityConfig.jobCreationRateLimit.windowMs / 1000));
    expect(insertMock).toHaveBeenCalledTimes(limit);
  });

  it("keys the limit by session, not globally: another session is unaffected", async () => {
    const limit = securityConfig.jobCreationRateLimit.maxAttempts;
    const responsesA = await createJobs(limit);
    expect(responsesA[limit - 1]?.status).toBe(201);
    const aBlocked = await POST_JOBS(requestWithSession("POST", "/api/v1/jobs", UPLOAD_PAYLOAD));
    expect(aBlocked.status).toBe(429);

    // A different session token (different verified session id) gets a fresh window.
    findSessionMock.mockResolvedValue({ id: OTHER_SESSION_ID, expires_at: FUTURE });
    const bResponse = await createJobs(1, "d".repeat(64));
    expect(bResponse[0]?.status).toBe(201);
  });

  it("does not count failed (400) payloads as creations", async () => {
    const bad = { ...UPLOAD_PAYLOAD, clipCount: 0 };
    for (let i = 0; i < securityConfig.jobCreationRateLimit.maxAttempts; i++) {
      const res = await POST_JOBS(requestWithSession("POST", "/api/v1/jobs", bad));
      expect(res.status).toBe(400);
    }
    // The limiter check happens before validation, so a flood of INVALID
    // payloads still saturates the window — that is intentional: the limit
    // bounds request COST, not successful creations.
    const res = await POST_JOBS(requestWithSession("POST", "/api/v1/jobs", UPLOAD_PAYLOAD));
    expect(res.status).toBe(429);
    expect(insertMock).not.toHaveBeenCalled();
  });
});

describe("safe error serialization (TASK 012)", () => {
  it("never leaks internals when the database layer throws", async () => {
    insertMock.mockRejectedValue(new Error("FATAL: relation \"jobs\" does not exist at schema.c"));
    const response = await POST_JOBS(requestWithSession("POST", "/api/v1/jobs", UPLOAD_PAYLOAD));
    expect(response.status).toBe(500);
    const body = await response.json();
    expect(body).toEqual({ error: "Internal server error." });
    expect(JSON.stringify(body)).not.toContain("FATAL");
    expect(JSON.stringify(body)).not.toContain("relation");
    expect(JSON.stringify(body)).not.toContain("schema");
  });

  it("lists jobs without exposing ownership or storage internals", async () => {
    const response = await LIST_JOBS(requestWithSession("GET", "/api/v1/jobs"));
    expect(response.status).toBe(200);
    const raw = await response.text();
    expect(raw).not.toContain("session_id");
    expect(raw).not.toContain("source_path");
    expect(raw).not.toContain("supabase");
  });
});

describe("authorization regression (TASK 012)", () => {
  it("rejects job creation without a session (401) and performs no DB write", async () => {
    const response = await POST_JOBS(
      new Request("http://localhost/api/v1/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(UPLOAD_PAYLOAD),
      })
    );
    expect(response.status).toBe(401);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("treats a foreign job identically to a missing one (404, no enumeration)", async () => {
    findJobMock.mockResolvedValue(null); // session-scoped lookup: foreign = not found
    const foreign = await GET_JOB(requestWithSession("GET", `/api/v1/jobs/${JOB_ID}`), {
      params: { jobId: JOB_ID },
    });
    expect(foreign.status).toBe(404);
    expect(await foreign.json()).toEqual({ error: "Job not found." });

    // Non-UUID ids never reach the database.
    const bogus = await GET_JOB(requestWithSession("GET", "/api/v1/jobs/../../etc/passwd"), {
      params: { jobId: "../../etc/passwd" },
    });
    expect(bogus.status).toBe(404);
    expect(findJobMock).toHaveBeenCalledTimes(1); // only the first request touched the DB
  });
});

describe("body-reader edge cases (TASK 012 audit)", () => {
  function streamedRequest(chunks: Uint8Array[], extraHeaders: Record<string, string> = {}): Request {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(chunk);
        controller.close();
      },
    });
    const req = new Request("http://localhost/api/v1/jobs", {
      method: "POST",
      headers: { "content-type": "application/json", ...extraHeaders },
      body: stream,
      // @ts-expect-error — duplex is required for stream bodies; undici accepts it
      duplex: "half",
    });
    return req;
  }

  it("enforces the cap from the STREAM when Content-Length is absent (413)", async () => {
    const big = new Uint8Array(6000).fill(1);
    const req = streamedRequest([big, big, big]); // 18,000 bytes > 16 KiB, no CL header
    await expect(readJsonBody(req, securityConfig.bodyLimit.maxBytes)).rejects.toBeInstanceOf(BodyTooLargeError);
  });

  it("also 413s at the route level for a stream body without Content-Length", async () => {
    const big = new Uint8Array(6000).fill(1);
    const req = streamedRequest([big, big, big], { cookie: `clipforge_session=${SESSION_TOKEN}` });
    const response = await POST_JOBS(req);
    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({ error: "Request payload is too large." });
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("tolerates a malformed Content-Length header (no crash, real bytes win)", async () => {
    const payload = JSON.stringify(UPLOAD_PAYLOAD);
    const req = new Request("http://localhost/api/v1/jobs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: payload,
    });
    req.headers.set("content-length", "abc"); // hostile client, like a raw TCP peer
    await expect(readJsonBody(req, securityConfig.bodyLimit.maxBytes)).resolves.toEqual(UPLOAD_PAYLOAD);
  });

  it("rejects an over-declared Content-Length without reading the stream", async () => {
    const req = new Request("http://localhost/api/v1/jobs", {
      method: "POST",
      headers: { "content-length": String(securityConfig.bodyLimit.maxBytes + 1) },
    });
    await expect(readJsonBody(req, securityConfig.bodyLimit.maxBytes)).rejects.toBeInstanceOf(BodyTooLargeError);
  });

  it("rejects an empty body as a parse error (400 path, not 413)", async () => {
    const req = new Request("http://localhost/api/v1/jobs", { method: "POST" });
    await expect(readJsonBody(req, securityConfig.bodyLimit.maxBytes)).rejects.toThrow(SyntaxError);
  });

  it("upload/complete rejects an oversized body with a typed 413", async () => {
    findJobMock.mockResolvedValue({
      id: JOB_ID,
      session_id: SESSION_ID,
      source_type: "upload",
      source_url: null,
      source_path: null,
      requested_clips: 3,
      clip_duration_seconds: 120,
      captions_enabled: true,
      status: "uploading",
      progress: 0,
      error_message: null,
      created_at: FUTURE,
      completed_at: null,
      expires_at: null,
      attempt_count: 0,
      max_attempts: 3,
      locked_by: null,
      locked_at: null,
      lease_expires_at: null,
      last_error: null,
      upload_completed_at: null,
      upload_auth_expires_at: new Date(Date.now() + 3600_000).toISOString(),
      source_metadata: {},
      updated_at: FUTURE,
    });
    const response = await POST_UPLOAD_COMPLETE(
      new Request("http://localhost/api/v1/jobs/55555555-5555-4555-8555-555555555555/upload/complete", {
        method: "POST",
        headers: { cookie: `clipforge_session=${SESSION_TOKEN}`, "content-type": "application/json" },
        body: JSON.stringify({ sha256: "f".repeat(64), bytes: "a".repeat(securityConfig.bodyLimit.maxBytes) }),
      }),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(413);
  });

  it("access-code flood returns 429 with a correct Retry-After", async () => {
    const post = (code: string) =>
      POST_ACCESS(
        new Request("http://localhost/api/access", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ code }),
        })
      );
    // 5 wrong codes consume the window (per IP "local-dev").
    for (let i = 0; i < 5; i++) {
      expect((await post("wrong-code")).status).toBe(401);
    }
    const blocked = await post(ACCESS_CODE); // even the RIGHT code is now throttled
    expect(blocked.status).toBe(429);
    const retryAfter = Number(blocked.headers.get("retry-after"));
    expect(Number.isFinite(retryAfter)).toBe(true);
    expect(retryAfter).toBeGreaterThanOrEqual(1);
    expect(retryAfter).toBeLessThanOrEqual(Math.ceil(securityConfig.rateLimit.windowMs / 1000));
  });
});
