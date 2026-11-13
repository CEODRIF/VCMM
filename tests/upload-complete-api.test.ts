/**
 * Phase 3 — POST /api/v1/jobs/[jobId]/upload/complete
 *
 * Verifies the anti-fake-completion contract: the browser can never mark a
 * job completed unless the server sees the real object in the private bucket
 * with the declared size, the authorization is unexpired, and the guarded
 * uploading → queued transition succeeds (or was already done — idempotent).
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
  buildSourcePath: (jobId: string, generatedFilename: string) =>
    `sources/${jobId}/original/${generatedFilename}`,
}));

import { POST } from "@/app/api/v1/jobs/[jobId]/upload/complete/route";
import { findSessionByTokenHash } from "@/lib/db/sessions";
import { findJobForSession, markUploadComplete } from "@/lib/db/jobs";
import { verifySourceObject } from "@/lib/storage/objects";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const findJobMock = findJobForSession as ReturnType<typeof vi.fn>;
const markCompleteMock = markUploadComplete as ReturnType<typeof vi.fn>;
const verifyMock = verifySourceObject as ReturnType<typeof vi.fn>;

const JOB_ID = "22222222-2222-4222-8222-222222222222";
const SESSION_ID = "33333333-3333-4333-8333-333333333333";
const FUTURE = new Date(Date.now() + 3600_000).toISOString();
const PAST = new Date(Date.now() - 3600_000).toISOString();
const OBJECT_PATH = `sources/${JOB_ID}/original/deadbeef-0000-4000-8000-000000000000.mp4`;

function jobRow(overrides: Record<string, unknown> = {}) {
  return {
    id: JOB_ID,
    session_id: SESSION_ID,
    source_type: "upload",
    source_url: null,
    source_path: OBJECT_PATH,
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
    upload_auth_expires_at: FUTURE,
    source_metadata: {
      original_file_name: "lecture.mp4",
      declared_size_bytes: 1_000_000,
      declared_mime_type: "video/mp4",
    },
    updated_at: FUTURE,
    ...overrides,
  };
}

function requestWith(body: unknown, cookie?: string): Request {
  return new Request(`http://localhost/api/v1/jobs/${JOB_ID}/upload/complete`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(cookie ? { cookie } : {}),
    },
    body: JSON.stringify(body),
  });
}

function withSession() {
  const token = "c".repeat(64);
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
  return `clipforge_session=${token}`;
}

const validBody = { sizeBytes: 1_000_000 };

beforeEach(() => {
  vi.clearAllMocks();
  findJobMock.mockResolvedValue(jobRow());
  markCompleteMock.mockResolvedValue(1);
  verifyMock.mockResolvedValue({ name: "deadbeef-0000-4000-8000-000000000000.mp4", size: 1_000_000 });
});

describe("authorization & ownership", () => {
  it("rejects requests without a session (401)", async () => {
    const response = await POST(requestWith(validBody), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(401);
    expect(verifyMock).not.toHaveBeenCalled();
  });

  it("rejects a job owned by another session (404)", async () => {
    withSession();
    findJobMock.mockResolvedValue(null);
    const response = await POST(requestWith(validBody, withSession()), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(404);
    expect(verifyMock).not.toHaveBeenCalled();
  });

  it("rejects non-UUID job ids (404)", async () => {
    withSession();
    const response = await POST(requestWith(validBody, withSession()), {
      params: { jobId: "not-a-uuid" },
    });
    expect(response.status).toBe(404);
    expect(findJobMock).not.toHaveBeenCalled();
  });

  it("rejects non-upload jobs (400)", async () => {
    const cookie = withSession();
    findJobMock.mockResolvedValue(jobRow({ source_type: "youtube" }));
    const response = await POST(requestWith(validBody, cookie), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(400);
  });

  it("rejects a job in an invalid state for completion (409)", async () => {
    const cookie = withSession();
    findJobMock.mockResolvedValue(jobRow({ status: "validating", locked_by: "w1", locked_at: FUTURE, lease_expires_at: FUTURE }));
    const response = await POST(requestWith(validBody, cookie), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(409);
    expect(verifyMock).not.toHaveBeenCalled();
  });
});

describe("server-side verification (the browser cannot fake completion)", () => {
  it("rejects completion when the object is missing in the private bucket (409)", async () => {
    const cookie = withSession();
    verifyMock.mockResolvedValue(null);
    const response = await POST(requestWith(validBody, cookie), { params: { jobId: JOB_ID } });
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(409);
    expect(body.error).toMatch(/object not found/i);
    expect(markCompleteMock).not.toHaveBeenCalled();
  });

  it("rejects when the declared size does not match the real object (400)", async () => {
    const cookie = withSession();
    verifyMock.mockResolvedValue({ name: "x.mp4", size: 500_000 });
    const response = await POST(requestWith(validBody, cookie), { params: { jobId: JOB_ID } });
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(400);
    expect(body.error).toMatch(/size does not match/i);
    expect(markCompleteMock).not.toHaveBeenCalled();
  });

  it("rejects when the reported size does not match the real object (400)", async () => {
    const cookie = withSession();
    const response = await POST(requestWith({ sizeBytes: 999_999 }, cookie), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(400);
    expect(markCompleteMock).not.toHaveBeenCalled();
  });

  it("rejects an expired upload authorization (410)", async () => {
    const cookie = withSession();
    findJobMock.mockResolvedValue(jobRow({ upload_auth_expires_at: PAST }));
    const response = await POST(requestWith(validBody, cookie), { params: { jobId: JOB_ID } });
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(410);
    expect(body.error).toMatch(/expired/i);
    expect(verifyMock).not.toHaveBeenCalled();
  });

  it("rejects a malformed payload (400)", async () => {
    const cookie = withSession();
    const response = await POST(requestWith({ sizeBytes: -5 }, cookie), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(400);
  });
});

describe("atomic, idempotent finalization", () => {
  it("enqueues the job exactly once on a verified upload (200)", async () => {
    const cookie = withSession();
    // First read: the uploading row; after the guarded update the route
    // re-reads the fresh (queued) row.
    findJobMock.mockResolvedValueOnce(jobRow());
    findJobMock.mockResolvedValueOnce(jobRow({ status: "queued", upload_completed_at: FUTURE }));
    const response = await POST(requestWith(validBody, cookie), { params: { jobId: JOB_ID } });

    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      enqueued: boolean;
      alreadyCompleted: boolean;
      job: { status: string; enqueuedAt: string | null };
    };
    expect(body.enqueued).toBe(true);
    expect(body.alreadyCompleted).toBe(false);
    expect(body.job.status).toBe("queued");
    expect(body.job.enqueuedAt).toBe(FUTURE);
    expect(markCompleteMock).toHaveBeenCalledTimes(1);
    expect(markCompleteMock).toHaveBeenCalledWith(SESSION_ID, JOB_ID);
    // The public job shape never leaks storage paths or queue internals.
    expect(JSON.stringify(body)).not.toContain("original/");
    expect(JSON.stringify(body)).not.toContain("session_id");
    expect(JSON.stringify(body)).not.toContain("locked_by");
  });

  it("answers duplicate completion idempotently (200, no re-transition)", async () => {
    const cookie = withSession();
    findJobMock.mockResolvedValue(jobRow({ status: "queued", upload_completed_at: FUTURE }));
    const response = await POST(requestWith(validBody, cookie), { params: { jobId: JOB_ID } });
    const body = (await response.json()) as { enqueued: boolean; alreadyCompleted: boolean };
    expect(response.status).toBe(200);
    expect(body.enqueued).toBe(true);
    expect(body.alreadyCompleted).toBe(true);
    expect(markCompleteMock).not.toHaveBeenCalled();
    expect(verifyMock).not.toHaveBeenCalled(); // already finalized — no re-verification needed
  });

  it("treats a duplicate call after worker failure as idempotent success (upload did complete)", async () => {
    const cookie = withSession();
    findJobMock.mockResolvedValue(jobRow({ status: "failed", upload_completed_at: FUTURE, last_error: "corrupt_media: x" }));
    const response = await POST(requestWith(validBody, cookie), { params: { jobId: JOB_ID } });
    const body = (await response.json()) as { alreadyCompleted: boolean; job: { status: string } };
    expect(response.status).toBe(200);
    expect(body.alreadyCompleted).toBe(true);
    expect(body.job.status).toBe("failed");
  });

  it("resolves a lost race idempotently when the re-read shows completion", async () => {
    const cookie = withSession();
    markCompleteMock.mockResolvedValue(0); // someone else won the guarded update
    findJobMock.mockResolvedValueOnce(jobRow());
    findJobMock.mockResolvedValueOnce(jobRow({ status: "queued", upload_completed_at: FUTURE }));
    const response = await POST(requestWith(validBody, cookie), { params: { jobId: JOB_ID } });
    const body = (await response.json()) as { alreadyCompleted: boolean };
    expect(response.status).toBe(200);
    expect(body.alreadyCompleted).toBe(true);
  });

  it("answers 409 when the guarded update matched no row and the re-read shows no completion", async () => {
    const cookie = withSession();
    markCompleteMock.mockResolvedValue(0);
    findJobMock.mockResolvedValue(jobRow()); // still uploading, never completed
    const response = await POST(requestWith(validBody, cookie), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(409);
  });
});
