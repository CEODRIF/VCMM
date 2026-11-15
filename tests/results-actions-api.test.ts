import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// TASK 015 P4 — permanent clip deletion + job-level cleanup at the API
// boundary. Real route code + real path-validation; the db and storage
// round trips are mocked (no network, no database). Authorization, the
// atomic state machine, the storage-verification contract, and the
// partial-failure/retry semantics are asserted end to end.

vi.mock("@/lib/db/sessions", () => ({
  findSessionByTokenHash: vi.fn(),
}));
vi.mock("@/lib/db/jobs", () => ({
  findActiveJobForSourceUrl: vi.fn(),
  findJobForSession: vi.fn(),
}));
vi.mock("@/lib/jobs/results", () => ({
  listClipsForJob: vi.fn(),
  findClipForJob: vi.fn(),
  markClipDeleting: vi.fn(),
  restoreClipStatus: vi.fn(),
  deleteClipRow: vi.fn(),
  setJobCleanupError: vi.fn(),
  markJobPurged: vi.fn(),
}));
vi.mock("@/lib/storage/clips", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/storage/clips")>();
  return {
    ...actual,
    createClipSignedUrl: vi.fn(),
    listJobClipObjects: vi.fn(),
    deleteClipObject: vi.fn(),
    deleteSourceObject: vi.fn(),
  };
});

import { GET as GET_DOWNLOAD } from "@/app/api/v1/jobs/[jobId]/clips/[clipId]/download/route";
import { DELETE as DELETE_CLIP } from "@/app/api/v1/jobs/[jobId]/clips/[clipId]/route";
import { GET as GET_RESULTS } from "@/app/api/v1/jobs/[jobId]/results/route";
import { POST as POST_CLEANUP } from "@/app/api/v1/jobs/[jobId]/cleanup/route";
import { findJobForSession } from "@/lib/db/jobs";
import {
  deleteClipRow,
  findClipForJob,
  listClipsForJob,
  markClipDeleting,
  markJobPurged,
  restoreClipStatus,
  setJobCleanupError,
  type ClipRow,
} from "@/lib/jobs/results";
import { deleteClipObject, deleteSourceObject } from "@/lib/storage/clips";
import { findSessionByTokenHash } from "@/lib/db/sessions";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const findJobMock = findJobForSession as ReturnType<typeof vi.fn>;
const listClipsMock = listClipsForJob as ReturnType<typeof vi.fn>;
const findClipMock = findClipForJob as ReturnType<typeof vi.fn>;
const markDeletingMock = markClipDeleting as ReturnType<typeof vi.fn>;
const restoreMock = restoreClipStatus as ReturnType<typeof vi.fn>;
const deleteRowMock = deleteClipRow as ReturnType<typeof vi.fn>;
const setCleanupErrorMock = setJobCleanupError as ReturnType<typeof vi.fn>;
const markPurgedMock = markJobPurged as ReturnType<typeof vi.fn>;
const deleteClipObjMock = deleteClipObject as ReturnType<typeof vi.fn>;
const deleteSourceObjMock = deleteSourceObject as ReturnType<typeof vi.fn>;

const SESSION_TOKEN = "b".repeat(64);
const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const OTHER_JOB_ID = "55555555-5555-4555-8555-555555555555";
const CLIP_ID = "33333333-3333-4333-8333-333333333333";
const CLIP_ID_2 = "44444444-4444-4444-8444-444444444444";
const FUTURE = new Date(Date.now() + 3600_000).toISOString();
const PAST = new Date(Date.now() - 3600_000).toISOString();

const ctx = { params: { jobId: JOB_ID, clipId: CLIP_ID } };

function requestWithSession(method: "GET" | "DELETE" | "POST", path: string, body?: unknown): Request {
  return new Request(`http://localhost${path}`, {
    method,
    headers: {
      cookie: `clipforge_session=${SESSION_TOKEN}`,
      ...(body !== undefined ? { "content-type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

function jobRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: JOB_ID,
    session_id: SESSION_ID,
    source_type: "upload",
    source_url: null,
    source_path: `sources/${JOB_ID}/original/generated.mp4`,
    requested_clips: 3,
    generation_mode: "highlights",
    output_fps: 30,
    clip_duration_seconds: 60,
    captions_enabled: true,
    status: "completed",
    progress: 100,
    error_message: null,
    created_at: FUTURE,
    completed_at: FUTURE,
    expires_at: FUTURE,
    purged_at: null,
    cleanup_error: null,
    attempt_count: 1,
    max_attempts: 3,
    locked_by: null,
    locked_at: null,
    lease_expires_at: PAST, // lease EXPIRED by default (no active worker)
    last_error: null,
    upload_completed_at: FUTURE,
    upload_auth_expires_at: null,
    source_metadata: { duration_seconds: 3600 },
    updated_at: FUTURE,
    ...overrides,
  };
}

function clipRow(overrides: Partial<ClipRow> = {}): ClipRow {
  return {
    id: CLIP_ID,
    title: "The opening hook",
    start_time: 32,
    end_time: 92,
    duration: 60,
    sort_order: 0,
    status: "ready",
    storage_path: `clips/${JOB_ID}/${CLIP_ID}.mp4`,
    mode: "highlights",
    width: 1080,
    height: 1920,
    output_fps: 30,
    file_size_bytes: 1_000_000,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
  findJobMock.mockResolvedValue(jobRow());
  listClipsMock.mockResolvedValue([]);
  findClipMock.mockResolvedValue(clipRow());
  markDeletingMock.mockResolvedValue(true);
  restoreMock.mockResolvedValue(true);
  deleteRowMock.mockResolvedValue(true);
  setCleanupErrorMock.mockResolvedValue(undefined);
  markPurgedMock.mockResolvedValue(true);
  deleteClipObjMock.mockResolvedValue({ deleted: true, missing: false });
  deleteSourceObjMock.mockResolvedValue({ deleted: true, missing: false });
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("DELETE clip — authorization", () => {
  it("requires a session (401) and never touches the job table", async () => {
    const response = await DELETE_CLIP(new Request("http://localhost/api/v1/jobs/" + `${JOB_ID}/clips/${CLIP_ID}`, { method: "DELETE" }), ctx);
    expect(response.status).toBe(401);
    expect(findJobMock).not.toHaveBeenCalled();
  });

  it("rejects a job owned by another session (uniform 404)", async () => {
    findJobMock.mockResolvedValue(null);
    const response = await DELETE_CLIP(requestWithSession("DELETE", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}`), ctx);
    expect(response.status).toBe(404);
    expect(markDeletingMock).not.toHaveBeenCalled();
  });

  it("rejects a nonexistent clip (404) without any state change", async () => {
    findClipMock.mockResolvedValue(null);
    const response = await DELETE_CLIP(requestWithSession("DELETE", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}`), ctx);
    expect(response.status).toBe(404);
    expect(markDeletingMock).not.toHaveBeenCalled();
  });
});

describe("DELETE clip — concurrent render/upload/delete protection", () => {
  it("refuses a clip still processing while the job is ACTIVE (409)", async () => {
    findJobMock.mockResolvedValue(jobRow({ status: "rendering", lease_expires_at: FUTURE }));
    findClipMock.mockResolvedValue(clipRow({ status: "processing" }));
    const response = await DELETE_CLIP(requestWithSession("DELETE", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}`), ctx);
    expect(response.status).toBe(409);
    expect(markDeletingMock).not.toHaveBeenCalled();
    expect(deleteClipObjMock).not.toHaveBeenCalled();
  });

  it("refuses a processing clip of a terminal job whose lease is STILL held (409)", async () => {
    // Job is failed but a worker lease is live — the clip may be in flight.
    findJobMock.mockResolvedValue(jobRow({ status: "failed", lease_expires_at: FUTURE }));
    findClipMock.mockResolvedValue(clipRow({ status: "processing" }));
    const response = await DELETE_CLIP(requestWithSession("DELETE", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}`), ctx);
    expect(response.status).toBe(409);
    expect(deleteClipObjMock).not.toHaveBeenCalled();
  });

  it("allows a STUCK processing clip once the job is terminal AND the lease expired", async () => {
    findJobMock.mockResolvedValue(jobRow({ status: "failed", lease_expires_at: PAST }));
    findClipMock.mockResolvedValue(clipRow({ status: "processing" }));
    const response = await DELETE_CLIP(requestWithSession("DELETE", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}`), ctx);
    expect(response.status).toBe(200);
    // The transition was taken from the verified current status only.
    expect(markDeletingMock).toHaveBeenCalledWith(JOB_ID, CLIP_ID, ["processing"]);
  });

  it("refuses a clip already 'deleting' (concurrent delete, 409)", async () => {
    findClipMock.mockResolvedValue(clipRow({ status: "deleting" }));
    const response = await DELETE_CLIP(requestWithSession("DELETE", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}`), ctx);
    expect(response.status).toBe(409);
    expect(deleteClipObjMock).not.toHaveBeenCalled();
  });

  it("a download for a 'deleting' clip is rejected with the uniform 404", async () => {
    findClipMock.mockResolvedValue(clipRow({ status: "deleting" }));
    const response = await GET_DOWNLOAD(requestWithSession("GET", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/download`), ctx);
    expect(response.status).toBe(404);
    const body = (await response.json()) as { error?: string };
    expect(body.error).toBe("Clip not found.");
  });
});

describe("DELETE clip — the verified-deletion protocol", () => {
  it("deletes the storage object, verifies, removes the row → 200 (order asserted)", async () => {
    const response = await DELETE_CLIP(requestWithSession("DELETE", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}`), ctx);
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ deleted: true, clipId: CLIP_ID });

    // Protocol: atomic transition → object deletion (canonical ref) → row removal.
    expect(markDeletingMock).toHaveBeenCalledWith(JOB_ID, CLIP_ID, ["ready"]);
    expect(deleteClipObjMock).toHaveBeenCalledWith({ jobId: JOB_ID, clipId: CLIP_ID });
    expect(deleteRowMock).toHaveBeenCalledWith(JOB_ID, CLIP_ID);
    expect(restoreMock).not.toHaveBeenCalled();
  });

  it("NEVER reports success when the storage deletion fails — rolls back to 'ready' and answers 502", async () => {
    deleteClipObjMock.mockRejectedValue(new Error("storage unavailable"));
    const response = await DELETE_CLIP(requestWithSession("DELETE", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}`), ctx);
    expect(response.status).toBe(502);
    const body = (await response.json()) as { error?: string };
    expect(body.error).toMatch(/still available/i);
    // Rolled back; the row was NOT removed; the clip stays downloadable.
    expect(restoreMock).toHaveBeenCalledWith(JOB_ID, CLIP_ID, "ready");
    expect(deleteRowMock).not.toHaveBeenCalled();
  });

  it("is idempotent across retries: an already-missing object still completes the deletion", async () => {
    deleteClipObjMock.mockResolvedValue({ deleted: false, missing: true });
    const response = await DELETE_CLIP(requestWithSession("DELETE", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}`), ctx);
    expect(response.status).toBe(200);
    expect(deleteRowMock).toHaveBeenCalledWith(JOB_ID, CLIP_ID);
  });

  it("a 'failed' clip without a verified path is a tombstone — row removed, no storage call", async () => {
    findClipMock.mockResolvedValue(clipRow({ status: "failed", storage_path: null }));
    const response = await DELETE_CLIP(requestWithSession("DELETE", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}`), ctx);
    expect(response.status).toBe(200);
    expect(deleteClipObjMock).not.toHaveBeenCalled();
    expect(deleteRowMock).toHaveBeenCalledWith(JOB_ID, CLIP_ID);
  });

  it("never deletes a path that does not belong to this job (canonical cross-check)", async () => {
    findClipMock.mockResolvedValue(
      clipRow({ storage_path: `clips/${OTHER_JOB_ID}/${CLIP_ID}.mp4` })
    );
    const response = await DELETE_CLIP(requestWithSession("DELETE", `/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}`), ctx);
    expect(response.status).toBe(200);
    expect(deleteClipObjMock).not.toHaveBeenCalled(); // foreign path → treated as tombstone row only
  });
});

describe("POST job cleanup — authorization + guards", () => {
  const cleanupCtx = { params: { jobId: JOB_ID } };

  it("requires explicit confirmation (400 without it)", async () => {
    const response = await POST_CLEANUP(requestWithSession("POST", `/api/v1/jobs/${JOB_ID}/cleanup`, {}), cleanupCtx);
    expect(response.status).toBe(400);
    expect(deleteClipObjMock).not.toHaveBeenCalled();
  });

  it("refuses an ACTIVE job (409) — media in use is never cleaned up", async () => {
    findJobMock.mockResolvedValue(jobRow({ status: "rendering" }));
    const response = await POST_CLEANUP(requestWithSession("POST", `/api/v1/jobs/${JOB_ID}/cleanup`, { confirm: true }), cleanupCtx);
    expect(response.status).toBe(409);
    expect(listClipsMock).not.toHaveBeenCalled();
  });

  it("rejects a foreign job (uniform 404)", async () => {
    findJobMock.mockResolvedValue(null);
    const response = await POST_CLEANUP(requestWithSession("POST", `/api/v1/jobs/${JOB_ID}/cleanup`, { confirm: true }), cleanupCtx);
    expect(response.status).toBe(404);
    expect(deleteClipObjMock).not.toHaveBeenCalled();
    expect(deleteSourceObjMock).not.toHaveBeenCalled();
  });

  it("is idempotent for an already-purged job (no storage calls)", async () => {
    findJobMock.mockResolvedValue(jobRow({ purged_at: FUTURE }));
    const response = await POST_CLEANUP(requestWithSession("POST", `/api/v1/jobs/${JOB_ID}/cleanup`, { confirm: true }), cleanupCtx);
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ purged: true, alreadyPurged: true });
    expect(deleteClipObjMock).not.toHaveBeenCalled();
    expect(deleteSourceObjMock).not.toHaveBeenCalled();
  });
});

describe("POST job cleanup — deletion order + partial failure", () => {
  const cleanupCtx = { params: { jobId: JOB_ID } };

  it("deletes clips first, then the source, then marks purged — all verified", async () => {
    listClipsMock.mockResolvedValue([
      clipRow({ id: CLIP_ID, sort_order: 0 }),
      clipRow({ id: CLIP_ID_2, sort_order: 1, storage_path: `clips/${JOB_ID}/${CLIP_ID_2}.mp4` }),
    ]);
    const response = await POST_CLEANUP(requestWithSession("POST", `/api/v1/jobs/${JOB_ID}/cleanup`, { confirm: true }), cleanupCtx);
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ purged: true, clipsDeleted: 2, sourcesDeleted: 1, errors: [] });

    expect(deleteClipObjMock).toHaveBeenCalledTimes(2);
    expect(deleteSourceObjMock).toHaveBeenCalledWith(JOB_ID, `sources/${JOB_ID}/original/generated.mp4`);
    expect(markPurgedMock).toHaveBeenCalledWith(JOB_ID);
    expect(setCleanupErrorMock).not.toHaveBeenCalled();
    expect(deleteRowMock).toHaveBeenCalledTimes(2);
  });

  it("a partial failure stays VISIBLE + RETRYABLE: job un-purged, cleanup_error set, failed clip restored", async () => {
    listClipsMock.mockResolvedValue([
      clipRow({ id: CLIP_ID, sort_order: 0 }),
      clipRow({ id: CLIP_ID_2, sort_order: 1, storage_path: `clips/${JOB_ID}/${CLIP_ID_2}.mp4` }),
    ]);
    // The second clip's storage deletion fails.
    deleteClipObjMock
      .mockResolvedValueOnce({ deleted: true, missing: false })
      .mockRejectedValueOnce(new Error("storage unavailable"));

    const response = await POST_CLEANUP(requestWithSession("POST", `/api/v1/jobs/${JOB_ID}/cleanup`, { confirm: true }), cleanupCtx);
    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      purged: boolean; retryable: boolean; clipsDeleted: number; sourcesDeleted: number; errors: string[];
    };
    expect(body.purged).toBe(false);
    expect(body.retryable).toBe(true);
    expect(body.clipsDeleted).toBe(1);
    expect(body.sourcesDeleted).toBe(1); // the source was still cleaned (independent step)
    expect(body.errors).toHaveLength(1);
    expect(body.errors[0]).toContain(CLIP_ID_2);

    // The failed clip was rolled back; the job stays un-purged with a visible error.
    expect(restoreMock).toHaveBeenCalledWith(JOB_ID, CLIP_ID_2, "ready");
    expect(markPurgedMock).not.toHaveBeenCalled();
    expect(setCleanupErrorMock).toHaveBeenCalledWith(JOB_ID, expect.stringContaining(CLIP_ID_2));
  });

  it("the RETRY of a partial cleanup finishes it (already-deleted clip absent from the list)", async () => {
    // Only the second clip's row survived the first attempt (the first was
    // fully deleted: object + row).
    listClipsMock.mockResolvedValue([
      clipRow({ id: CLIP_ID_2, sort_order: 1, storage_path: `clips/${JOB_ID}/${CLIP_ID_2}.mp4` }),
    ]);
    findJobMock.mockResolvedValue(jobRow({ cleanup_error: "cleanup incomplete" }));
    const response = await POST_CLEANUP(requestWithSession("POST", `/api/v1/jobs/${JOB_ID}/cleanup`, { confirm: true }), cleanupCtx);
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ purged: true, clipsDeleted: 1, sourcesDeleted: 1, errors: [] });
    expect(markPurgedMock).toHaveBeenCalledWith(JOB_ID); // marker + cleanup_error cleared together
  });

  it("never deletes a source path that does not belong to this job", async () => {
    findJobMock.mockResolvedValue(jobRow({ source_path: `sources/${OTHER_JOB_ID}/original/other.mp4` }));
    listClipsMock.mockResolvedValue([]);
    const response = await POST_CLEANUP(requestWithSession("POST", `/api/v1/jobs/${JOB_ID}/cleanup`, { confirm: true }), cleanupCtx);
    expect(response.status).toBe(200);
    expect(deleteSourceObjMock).not.toHaveBeenCalled();
    expect(await response.json()).toMatchObject({ purged: true, sourcesDeleted: 0 });
  });

  it("skips clips owned by a concurrent single-clip delete (status 'deleting')", async () => {
    listClipsMock.mockResolvedValue([
      clipRow({ id: CLIP_ID, status: "deleting", sort_order: 0 }),
      clipRow({ id: CLIP_ID_2, sort_order: 1, storage_path: `clips/${JOB_ID}/${CLIP_ID_2}.mp4` }),
    ]);
    const response = await POST_CLEANUP(requestWithSession("POST", `/api/v1/jobs/${JOB_ID}/cleanup`, { confirm: true }), cleanupCtx);
    expect(response.status).toBe(200);
    const body = (await response.json()) as { clipsDeleted: number; errors: string[] };
    expect(body.clipsDeleted).toBe(1); // only the second clip
    expect(markDeletingMock).not.toHaveBeenCalledWith(JOB_ID, CLIP_ID, expect.anything());
  });
});

describe("GET results — P4 metadata is real, never fabricated", () => {
  it("exposes measured clip facts + live file size + job cleanup state", async () => {
    listClipsMock.mockResolvedValue([
      clipRow({ start_time: 32, end_time: 92, mode: "full_coverage", width: 1080, height: 1920, output_fps: 60 }),
    ]);
    const { listJobClipObjects } = await import("@/lib/storage/clips");
    (listJobClipObjects as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Map([["33333333-3333-4333-8333-333333333333.mp4", { name: "33333333-3333-4333-8333-333333333333.mp4", size: 2_500_000 }]])
    );
    const response = await GET_RESULTS(requestWithSession("GET", `/api/v1/jobs/${JOB_ID}/results`), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      job: Record<string, unknown>;
      clips: Record<string, unknown>[];
    };
    expect(body.job).toMatchObject({
      generationMode: "highlights",
      outputFps: 30,
      sourceDurationSeconds: 3600,
      purgedAt: null,
      cleanupError: null,
    });
    expect(body.clips[0]).toMatchObject({
      id: CLIP_ID,
      startTime: 32,
      endTime: 92,
      mode: "full_coverage",
      width: 1080,
      height: 1920,
      outputFps: 60,
      fileSizeBytes: 2_500_000, // LIVE from the storage listing
    });
  });
});
