import { beforeEach, describe, expect, it, vi } from "vitest";

// Mock the database layer; route + validation + session logic stay real.
vi.mock("@/lib/db/sessions", () => ({
  findSessionByTokenHash: vi.fn(),
  createSessionRow: vi.fn(),
  deleteSessionRow: vi.fn(),
}));
vi.mock("@/lib/db/jobs", () => ({
  insertJob: vi.fn(),
  findJobForSession: vi.fn(),
  listJobsForSession: vi.fn(),
}));

import { POST as POST_JOBS, GET as LIST_JOBS } from "@/app/api/v1/jobs/route";
import { GET as GET_JOB } from "@/app/api/v1/jobs/[jobId]/route";
import { findSessionByTokenHash } from "@/lib/db/sessions";
import { findJobForSession, insertJob, listJobsForSession } from "@/lib/db/jobs";
import { sha256Hex } from "@/lib/security/crypto";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const insertMock = insertJob as ReturnType<typeof vi.fn>;
const findJobMock = findJobForSession as ReturnType<typeof vi.fn>;
const listMock = listJobsForSession as ReturnType<typeof vi.fn>;

const SESSION_TOKEN = "b".repeat(64);
const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const FUTURE = new Date(Date.now() + 3600_000).toISOString();
const JOB_ID = "22222222-2222-4222-8222-222222222222";

function requestWithSession(method: "GET" | "POST", path: string, body?: unknown): Request {
  return new Request(`http://localhost${path}`, {
    method,
    headers: {
      cookie: `clipforge_session=${SESSION_TOKEN}`,
      ...(body ? { "content-type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
  insertMock.mockResolvedValue({ id: JOB_ID });
  findJobMock.mockResolvedValue(null);
  listMock.mockResolvedValue([]);
});

describe("POST /api/v1/jobs — job creation", () => {
  const uploadPayload = {
    sourceType: "upload",
    fileName: "my video (final).mp4",
    clipCount: 3,
    clipDurationSeconds: 120,
    captionsEnabled: true,
  };
  const youtubePayload = {
    sourceType: "youtube",
    youtubeUrl: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    clipCount: 3,
    clipDurationSeconds: 120,
    captionsEnabled: true,
  };

  it("rejects requests without a session (401)", async () => {
    const response = await POST_JOBS(
      new Request("http://localhost/api/v1/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(uploadPayload),
      })
    );
    expect(response.status).toBe(401);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("rejects expired sessions (401)", async () => {
    findSessionMock.mockResolvedValue(null);
    const response = await POST_JOBS(requestWithSession("POST", "/api/v1/jobs", uploadPayload));
    expect(response.status).toBe(401);
  });

  it("creates an upload job in the 'uploading' state with a server-reserved path (201)", async () => {
    const response = await POST_JOBS(requestWithSession("POST", "/api/v1/jobs", uploadPayload));
    expect(response.status).toBe(201);

    const body = (await response.json()) as { job: { id: string; status: string } };
    // The service generates a server-side UUID; the response must be a valid one.
    expect(body.job.id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
    );
    // Phase 3: uploads wait for the verified file before entering the queue.
    expect(body.job.status).toBe("uploading");

    expect(insertMock).toHaveBeenCalledTimes(1);
    const call = insertMock.mock.calls[0];
    expect(call).toBeDefined();
    const input = call?.[0] as Record<string, unknown>;
    // The id the DB receives is the same id the client got.
    expect(input.id).toBe(body.job.id);
    expect(input.sessionId).toBe(SESSION_ID);
    expect(input.sourceType).toBe("upload");
    expect(input.requestedClips).toBe(3);
    expect(input.clipDurationSeconds).toBe(120);
    expect(input.captionsEnabled).toBe(true);
    expect(input.initialStatus).toBe("uploading");
    // Reserved location per job — the exact object path is pinned later by
    // the authorization endpoint (never client-supplied).
    expect(input.sourcePath).toBe(`sources/${body.job.id}/reserved`);

    // No private storage paths in the response.
    expect(JSON.stringify(body)).not.toContain("source_path");
    expect(JSON.stringify(body)).not.toContain("storage");
    expect(JSON.stringify(body)).not.toContain("sources/");
  });

  it("refuses YouTube jobs with a clear 503 when no authorized provider is configured", async () => {
    const response = await POST_JOBS(requestWithSession("POST", "/api/v1/jobs", youtubePayload));
    expect(response.status).toBe(503);
    const body = (await response.json()) as { error: string };
    expect(body.error).toMatch(/provider is not configured/i);
    expect(body.error).toMatch(/direct video file upload is available/i);
    // No job row must be created for an unavailable provider.
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("rejects invalid payloads with 400", async () => {
    const cases: Array<Record<string, unknown>> = [
      { ...uploadPayload, clipCount: 0 },
      { ...uploadPayload, clipCount: 11 },
      { ...uploadPayload, clipDurationSeconds: 59 },
      { ...uploadPayload, clipDurationSeconds: 181 },
      { ...uploadPayload, clipDurationSeconds: 90.5 },
      { ...youtubePayload, youtubeUrl: "https://example.com/video" },
      { sourceType: "upload", clipCount: 1, clipDurationSeconds: 60, captionsEnabled: true },
      { ...uploadPayload, fileName: "../escape.mp4" },
    ];
    for (const payload of cases) {
      const response = await POST_JOBS(requestWithSession("POST", "/api/v1/jobs", payload));
      expect(response.status, JSON.stringify(payload)).toBe(400);
    }
    expect(insertMock).not.toHaveBeenCalled();
  });
});

describe("GET /api/v1/jobs — session-scoped list", () => {
  it("lists only jobs of the current session", async () => {
    listMock.mockResolvedValue([
      {
        id: JOB_ID,
        session_id: SESSION_ID,
        source_type: "youtube",
        source_url: "https://youtube.com/watch?v=x",
        source_path: "jobs/x/uploads/f.mp4",
        requested_clips: 2,
        clip_duration_seconds: 90,
        captions_enabled: true,
        status: "queued",
        progress: 0,
        error_message: null,
        created_at: FUTURE,
        completed_at: null,
        expires_at: null,
      },
    ]);
    const response = await LIST_JOBS(requestWithSession("GET", "/api/v1/jobs"));
    expect(response.status).toBe(200);

    const body = (await response.json()) as { jobs: Array<Record<string, unknown>> };
    expect(body.jobs).toHaveLength(1);
    expect(body.jobs[0]?.id).toBe(JOB_ID);
    // Ownership and storage internals are never exposed.
    expect(body.jobs[0]).not.toHaveProperty("session_id");
    expect(body.jobs[0]).not.toHaveProperty("source_path");

    expect(listMock).toHaveBeenCalledWith(SESSION_ID, 20);
  });

  it("requires a session (401)", async () => {
    const response = await LIST_JOBS(new Request("http://localhost/api/v1/jobs"));
    expect(response.status).toBe(401);
  });
});

describe("GET /api/v1/jobs/[jobId] — status + anti-enumeration", () => {
  const ownJob = {
    id: JOB_ID,
    session_id: SESSION_ID,
    source_type: "youtube" as const,
    source_url: "https://youtube.com/watch?v=x",
    source_path: "jobs/x/uploads/f.mp4",
    requested_clips: 2,
    clip_duration_seconds: 90,
    captions_enabled: true,
    status: "queued" as const,
    progress: 0,
    error_message: null,
    created_at: FUTURE,
    completed_at: null,
    expires_at: null,
  };

  it("returns the status of an owned job (200)", async () => {
    findJobMock.mockResolvedValue(ownJob);
    const response = await GET_JOB(requestWithSession("GET", `/api/v1/jobs/${JOB_ID}`), {
      params: { jobId: JOB_ID },
    });
    expect(response.status).toBe(200);
    const body = (await response.json()) as { job: Record<string, unknown> };
    expect(body.job.id).toBe(JOB_ID);
    expect(body.job.status).toBe("queued");
    expect(body.job).not.toHaveProperty("source_path");
  });

  it("returns 404 for a job owned by another session (indistinguishable from missing)", async () => {
    // The DB layer is session-scoped: a foreign job simply does not come back.
    findJobMock.mockResolvedValue(null);
    const response = await GET_JOB(requestWithSession("GET", `/api/v1/jobs/${JOB_ID}`), {
      params: { jobId: JOB_ID },
    });
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Job not found." });
  });

  it("rejects non-UUID ids without a database round trip (404)", async () => {
    const response = await GET_JOB(requestWithSession("GET", "/api/v1/jobs/not-a-uuid"), {
      params: { jobId: "not-a-uuid" },
    });
    expect(response.status).toBe(404);
    expect(findJobMock).not.toHaveBeenCalled();
  });

  it("requires a session (401)", async () => {
    const response = await GET_JOB(new Request(`http://localhost/api/v1/jobs/${JOB_ID}`), {
      params: { jobId: JOB_ID },
    });
    expect(response.status).toBe(401);
  });
});

describe("session token hygiene", () => {
  it("the database is always queried by token hash, never raw token", async () => {
    findSessionMock.mockResolvedValue(null);
    await POST_JOBS(requestWithSession("POST", "/api/v1/jobs", {
      sourceType: "youtube",
      youtubeUrl: "https://youtube.com/watch?v=x",
      clipCount: 1,
      clipDurationSeconds: 60,
      captionsEnabled: true,
    }));
    expect(findSessionMock).toHaveBeenCalledWith(sha256Hex(SESSION_TOKEN));
    expect(findSessionMock).not.toHaveBeenCalledWith(SESSION_TOKEN);
  });
});
