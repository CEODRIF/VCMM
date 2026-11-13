/**
 * Phase 3 — POST /api/v1/jobs/[jobId]/upload/authorize
 *
 * The route logic under test is real (session verification, ownership,
 * state guards, extension/MIME/size validation, rate limiting). Only the
 * database layer and the Supabase Storage client boundary are mocked.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

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
}));
vi.mock("@/lib/storage/objects", () => ({
  createSourceUploadUrl: vi.fn(),
  verifySourceObject: vi.fn(),
  // Pure server-side path builder — kept real (same template as production).
  buildSourcePath: (jobId: string, generatedFilename: string) =>
    `sources/${jobId}/original/${generatedFilename}`,
}));

import { POST } from "@/app/api/v1/jobs/[jobId]/upload/authorize/route";
import { uploadConfig } from "@/config/uploads";
import { findSessionByTokenHash } from "@/lib/db/sessions";
import { findJobForSession, setUploadAuthorization } from "@/lib/db/jobs";
import { createSourceUploadUrl } from "@/lib/storage/objects";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const findJobMock = findJobForSession as ReturnType<typeof vi.fn>;
const setAuthMock = setUploadAuthorization as ReturnType<typeof vi.fn>;
const signedUrlMock = createSourceUploadUrl as ReturnType<typeof vi.fn>;

const JOB_ID = "22222222-2222-4222-8222-222222222222";
const FUTURE = new Date(Date.now() + 3600_000).toISOString();
const SIGNED_URL = "https://storage.example.com/object/upload/token=fake";

let sessionCounter = 0;

/** A valid owning job in the 'uploading' state (fresh session per call). */
function ownUploadingJob(overrides: Record<string, unknown> = {}) {
  const sessionId = `33333333-3333-4333-8333-${String(333333333000 + sessionCounter++).padStart(12, "0")}`;
  findSessionMock.mockResolvedValue({ id: sessionId, expires_at: FUTURE });
  findJobMock.mockResolvedValue({
    id: JOB_ID,
    session_id: sessionId,
    source_type: "upload",
    source_url: null,
    source_path: `sources/${JOB_ID}/reserved`,
    requested_clips: 1,
    clip_duration_seconds: 60,
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
    upload_auth_expires_at: null,
    source_metadata: {},
    updated_at: FUTURE,
    ...overrides,
  });
  return sessionId;
}

function requestFor(path: string, body: unknown, cookie?: string): Request {
  return new Request(`http://localhost${path}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(cookie ? { cookie } : {}),
    },
    body: JSON.stringify(body),
  });
}

const validBody = { fileName: "lecture.mp4", sizeBytes: 1_000_000, mimeType: "video/mp4" };

beforeEach(() => {
  vi.clearAllMocks();
  setAuthMock.mockResolvedValue(1);
  signedUrlMock.mockResolvedValue({ signedUrl: SIGNED_URL });
});

describe("authorization & ownership", () => {
  it("rejects requests without a session (401)", async () => {
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, validBody),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(401);
    expect(findJobMock).not.toHaveBeenCalled();
  });

  it("rejects expired sessions (401)", async () => {
    const sessionId = ownUploadingJob();
    findSessionMock.mockResolvedValue(null);
    const token = "c".repeat(64);
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, validBody, `clipforge_session=${token}`),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(401);
    expect(findJobMock).not.toHaveBeenCalled();
    void sessionId;
  });

  it("rejects non-UUID job ids without a database round trip (404)", async () => {
    ownUploadingJob();
    const response = await POST(
      requestFor(`/api/v1/jobs/not-a-uuid/upload/authorize`, validBody, "clipforge_session=x"),
      { params: { jobId: "not-a-uuid" } }
    );
    expect(response.status).toBe(404);
    expect(findJobMock).not.toHaveBeenCalled();
  });

  it("rejects a job owned by another session (404, indistinguishable from missing)", async () => {
    ownUploadingJob();
    findJobMock.mockResolvedValue(null);
    const token = "c".repeat(64);
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, validBody, `clipforge_session=${token}`),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(404);
  });

  it("rejects non-upload jobs (400)", async () => {
    ownUploadingJob({ source_type: "youtube" });
    const token = "c".repeat(64);
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, validBody, `clipforge_session=${token}`),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(400);
  });

  it("rejects jobs that left the uploading state (409)", async () => {
    ownUploadingJob({ status: "queued", upload_completed_at: FUTURE });
    const token = "c".repeat(64);
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, validBody, `clipforge_session=${token}`),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(409);
  });
});

describe("file validation", () => {
  it("rejects an unsupported file extension (415)", async () => {
    ownUploadingJob();
    const token = "c".repeat(64);
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, { ...validBody, fileName: "notes.txt" }, `clipforge_session=${token}`),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(415);
    expect(signedUrlMock).not.toHaveBeenCalled();
  });

  it("rejects a file with no extension (415)", async () => {
    ownUploadingJob();
    const token = "c".repeat(64);
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, { ...validBody, fileName: "noextension" }, `clipforge_session=${token}`),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(415);
  });

  it("rejects an unsupported MIME type (415)", async () => {
    ownUploadingJob();
    const token = "c".repeat(64);
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, { ...validBody, mimeType: "application/pdf" }, `clipforge_session=${token}`),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(415);
  });

  it("rejects MIME/extension spoofing (mp4 named as webm) with 400", async () => {
    ownUploadingJob();
    const token = "c".repeat(64);
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, { ...validBody, mimeType: "video/webm" }, `clipforge_session=${token}`),
      { params: { jobId: JOB_ID } }
    );
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(400);
    expect(body.error).toMatch(/do not match/i);
    expect(signedUrlMock).not.toHaveBeenCalled();
  });

  it("rejects path traversal in the file name (400)", async () => {
    ownUploadingJob();
    const token = "c".repeat(64);
    for (const fileName of ["../escape.mp4", "a/b.mp4", "a\\b.mp4"]) {
      const response = await POST(
        requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, { ...validBody, fileName }, `clipforge_session=${token}`),
        { params: { jobId: JOB_ID } }
      );
      expect(response.status, fileName).toBe(400);
    }
  });

  it("rejects oversized uploads against the configured limit (413)", async () => {
    vi.stubEnv("MAX_UPLOAD_SIZE_BYTES", "1024");
    try {
      ownUploadingJob();
      const token = "c".repeat(64);
      const response = await POST(
        requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, { ...validBody, sizeBytes: 2048 }, `clipforge_session=${token}`),
        { params: { jobId: JOB_ID } }
      );
      const body = (await response.json()) as { error: string };
      expect(response.status).toBe(413);
      expect(body.error).toMatch(/too large/i);
      expect(signedUrlMock).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("rejects malformed payloads (400)", async () => {
    ownUploadingJob();
    const token = "c".repeat(64);
    for (const body of [{}, { fileName: "a.mp4", sizeBytes: -1, mimeType: "video/mp4" }, "not-json"]) {
      const response = await POST(
        requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, body, `clipforge_session=${token}`),
        { params: { jobId: JOB_ID } }
      );
      expect(response.status, JSON.stringify(body)).toBe(400);
    }
  });
});

describe("rate limiting", () => {
  it("returns 429 after the per-minute authorization budget is exhausted", async () => {
    ownUploadingJob();
    const token = "c".repeat(64);
    const cookie = `clipforge_session=${token}`;
    let lastStatus = 0;
    for (let i = 0; i < 11; i++) {
      const response = await POST(
        requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, validBody, cookie),
        { params: { jobId: JOB_ID } }
      );
      lastStatus = response.status;
    }
    expect(lastStatus).toBe(429);
    // 10 authorized, 11th denied — the signed-URL creator ran exactly 10 times.
    expect(signedUrlMock).toHaveBeenCalledTimes(10);
  });
});

describe("successful authorization", () => {
  it("returns only the upload information the client needs (200)", async () => {
    ownUploadingJob();
    const token = "c".repeat(64);
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, validBody, `clipforge_session=${token}`),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(200);

    const body = (await response.json()) as Record<string, unknown>;
    expect(body.uploadUrl).toBe(SIGNED_URL);
    expect(body.method).toBe("PUT");
    expect(body.contentType).toBe("video/mp4");
    expect(body.expiresInSeconds).toBeGreaterThan(0);
    expect(body.jobId).toBe(JOB_ID);
    expect(body.maxBytes).toBeGreaterThan(0);
    // The response never reveals the storage object layout.
    expect(JSON.stringify(body)).not.toContain("/original/");
    expect(JSON.stringify(body)).not.toContain("sources/");
    // No storage credentials leak.
    expect(JSON.stringify(body)).not.toContain("service_role");
  });

  it("pins a server-generated object path for the job (UUID-based, never client-supplied)", async () => {
    ownUploadingJob();
    const token = "c".repeat(64);
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, validBody, `clipforge_session=${token}`),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(200);

    expect(signedUrlMock).toHaveBeenCalledTimes(1);
    const signedPath = signedUrlMock.mock.calls[0]?.[0] as string;
    expect(signedPath).toMatch(
      new RegExp(`^sources/${JOB_ID}/original/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\.mp4$`)
    );
    expect(signedPath).not.toContain("lecture"); // client file name never reaches storage

    expect(setAuthMock).toHaveBeenCalledTimes(1);
    const call = setAuthMock.mock.calls[0] as unknown[];
    expect(call?.[0]).toMatch(/^33333333-/); // the verified session id
    expect(call?.[1]).toBe(JOB_ID);
    expect(call?.[2]).toBe(signedPath);
    // A future, bounded authorization expiry is recorded (default TTL 900s).
    const ttl = uploadConfig.uploadAuthTtlSeconds();
    const expires = new Date(String(call?.[3]));
    expect(expires.getTime()).toBeGreaterThan(Date.now());
    expect(expires.getTime() - Date.now()).toBeLessThanOrEqual(ttl * 1000 + 5_000);
    expect(call?.[4]).toEqual({ fileName: "lecture.mp4", sizeBytes: 1_000_000, mimeType: "video/mp4" });
  });

  it("answers 409 when the job left the uploading state mid-flight (race)", async () => {
    ownUploadingJob();
    setAuthMock.mockResolvedValue(0);
    const token = "c".repeat(64);
    const response = await POST(
      requestFor(`/api/v1/jobs/${JOB_ID}/upload/authorize`, validBody, `clipforge_session=${token}`),
      { params: { jobId: JOB_ID } }
    );
    expect(response.status).toBe(409);
  });
});
