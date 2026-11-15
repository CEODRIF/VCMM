/**
 * TASK 017 — POST /api/v1/jobs/[jobId]/cancel
 *
 * User cancellation of an upload that never finished. The guarded
 * 'uploading' → 'failed' transition makes double-cancel impossible and a
 * cancel racing upload-completion can never touch a row twice. The public
 * response never leaks storage paths or queue internals.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/db/sessions", () => ({
  findSessionByTokenHash: vi.fn(),
  createSessionRow: vi.fn(),
  deleteSessionRow: vi.fn(),
}));
vi.mock("@/lib/db/jobs", () => ({
  findJobForSession: vi.fn(),
  cancelUploadJob: vi.fn(),
}));

import { POST } from "@/app/api/v1/jobs/[jobId]/cancel/route";
import { findSessionByTokenHash } from "@/lib/db/sessions";
import { cancelUploadJob, findJobForSession } from "@/lib/db/jobs";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const findJobMock = findJobForSession as ReturnType<typeof vi.fn>;
const cancelMock = cancelUploadJob as ReturnType<typeof vi.fn>;

const JOB_ID = "22222222-2222-4222-8222-222222222222";
const SESSION_ID = "33333333-3333-4333-8333-333333333333";
const FUTURE = new Date(Date.now() + 3600_000).toISOString();

function jobRow(overrides: Record<string, unknown> = {}) {
  return {
    id: JOB_ID,
    session_id: SESSION_ID,
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
    purged_at: null,
    cleanup_error: null,
    attempt_count: 0,
    max_attempts: 3,
    locked_by: null,
    locked_at: null,
    lease_expires_at: null,
    last_error: null,
    upload_completed_at: null,
    upload_auth_expires_at: FUTURE,
    source_metadata: { original_file_name: "lecture.mp4", declared_size_bytes: 1_000_000 },
    updated_at: FUTURE,
    ...overrides,
  };
}

function request(cookie?: string): Request {
  return new Request(`http://localhost/api/v1/jobs/${JOB_ID}/cancel`, {
    method: "POST",
    headers: cookie ? { cookie } : undefined,
  });
}

function withSession(): string {
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
  return `clipforge_session=${"c".repeat(64)}`;
}

beforeEach(() => {
  vi.clearAllMocks();
  findJobMock.mockResolvedValue(jobRow());
  cancelMock.mockResolvedValue(1);
});

describe("authorization & validation", () => {
  it("rejects requests without a session (401)", async () => {
    const response = await POST(request(), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(401);
    expect(findJobMock).not.toHaveBeenCalled();
    expect(cancelMock).not.toHaveBeenCalled();
  });

  it("rejects a job owned by another session (404)", async () => {
    const cookie = withSession();
    findJobMock.mockResolvedValue(null);
    const response = await POST(request(cookie), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(404);
    expect(cancelMock).not.toHaveBeenCalled();
  });

  it("rejects non-UUID job ids (404) without touching the database", async () => {
    withSession();
    const response = await POST(request(withSession()), { params: { jobId: "not-a-uuid" } });
    expect(response.status).toBe(404);
    expect(findJobMock).not.toHaveBeenCalled();
  });

  it("rejects non-upload jobs (400)", async () => {
    const cookie = withSession();
    findJobMock.mockResolvedValue(jobRow({ source_type: "direct_url", status: "queued" }));
    const response = await POST(request(cookie), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(400);
    expect(cancelMock).not.toHaveBeenCalled();
  });
});

describe("state guard", () => {
  it("refuses to cancel a job that already finished processing (409)", async () => {
    const cookie = withSession();
    findJobMock.mockResolvedValue(jobRow({ status: "queued", upload_completed_at: FUTURE }));
    const response = await POST(request(cookie), { params: { jobId: JOB_ID } });
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(409);
    expect(body.error).toMatch(/no longer in progress/i);
    expect(cancelMock).not.toHaveBeenCalled();
  });

  it("refuses to cancel a failed/completed terminal job (409)", async () => {
    const cookie = withSession();
    findJobMock.mockResolvedValue(jobRow({ status: "failed", last_error: "upload timeout" }));
    const response = await POST(request(cookie), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(409);
    expect(cancelMock).not.toHaveBeenCalled();
  });
});

describe("atomic cancellation", () => {
  it("fails the uploading job and returns the public job (200)", async () => {
    const cookie = withSession();
    findJobMock.mockResolvedValueOnce(jobRow());
    findJobMock.mockResolvedValueOnce(jobRow({ status: "failed", last_error: "Cancelled by user" }));
    const response = await POST(request(cookie), { params: { jobId: JOB_ID } });

    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      cancelled: boolean;
      job: { status: string; errorMessage?: string };
    };
    expect(body.cancelled).toBe(true);
    expect(body.job.status).toBe("failed");
    expect(cancelMock).toHaveBeenCalledTimes(1);
    expect(cancelMock).toHaveBeenCalledWith(JOB_ID);
    // No internal fields leak.
    const raw = JSON.stringify(body);
    expect(raw).not.toContain("session_id");
    expect(raw).not.toContain("sources/");
    expect(raw).not.toContain("locked_by");
  });

  it("answers 409 (not an error) when the cancel loses the race to completion", async () => {
    const cookie = withSession();
    cancelMock.mockResolvedValue(0); // upload completed in the meantime
    findJobMock.mockResolvedValueOnce(jobRow());
    findJobMock.mockResolvedValueOnce(jobRow({ status: "queued", upload_completed_at: FUTURE }));
    const response = await POST(request(cookie), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(409);
    const body = (await response.json()) as { error: string };
    expect(body.error).toMatch(/queued/);
  });
});
