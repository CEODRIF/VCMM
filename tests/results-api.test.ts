import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the DB layer; route + ownership + storage-path logic stay real.
vi.mock("@/lib/db/sessions", () => ({
  findSessionByTokenHash: vi.fn(),
  createSessionRow: vi.fn(),
  deleteSessionRow: vi.fn(),
}));
vi.mock("@/lib/db/jobs", () => ({
  findJobForSession: vi.fn(),
}));
vi.mock("@/lib/jobs/results", () => ({
  listClipsForJob: vi.fn(),
  findClipForJob: vi.fn(),
}));
// Keep the pure path helpers real (validateClipPath, clipObjectPath);
// fake only the Storage round trips (signed URLs + metadata listing).
vi.mock("@/lib/storage/clips", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/storage/clips")>();
  return {
    ...actual,
    createClipSignedUrl: vi.fn(),
    listJobClipObjects: vi.fn(),
    fetchClipObjectStream: vi.fn(),
  };
});

import { GET as GET_RESULTS } from "@/app/api/v1/jobs/[jobId]/results/route";
import { GET as GET_PREVIEW } from "@/app/api/v1/jobs/[jobId]/clips/[clipId]/preview/route";
import { GET as GET_DOWNLOAD } from "@/app/api/v1/jobs/[jobId]/clips/[clipId]/download/route";
import { findSessionByTokenHash } from "@/lib/db/sessions";
import { findJobForSession } from "@/lib/db/jobs";
import { findClipForJob, listClipsForJob, type ClipRow } from "@/lib/jobs/results";
import { createClipSignedUrl, listJobClipObjects } from "@/lib/storage/clips";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const findJobMock = findJobForSession as ReturnType<typeof vi.fn>;
const listClipsMock = listClipsForJob as ReturnType<typeof vi.fn>;
const findClipMock = findClipForJob as ReturnType<typeof vi.fn>;
const signMock = createClipSignedUrl as ReturnType<typeof vi.fn>;
const listObjectsMock = listJobClipObjects as ReturnType<typeof vi.fn>;

const SESSION_TOKEN = "b".repeat(64);
const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const OTHER_JOB_ID = "55555555-5555-4555-8555-555555555555";
const CLIP_ID = "33333333-3333-4333-8333-333333333333";
const CLIP_ID_2 = "44444444-4444-4444-8444-444444444444";
const FUTURE = new Date(Date.now() + 3600_000).toISOString();
const EXPIRY = new Date(Date.now() + 30 * 86400_000).toISOString();

function requestWithSession(path: string): Request {
  return new Request(`http://localhost${path}`, {
    method: "GET",
    headers: { cookie: `clipforge_session=${SESSION_TOKEN}` },
  });
}

function jobRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: JOB_ID,
    session_id: SESSION_ID,
    source_type: "upload",
    source_url: null,
    source_path: `sources/${JOB_ID}/original/file.mp4`,
    requested_clips: 2,
    clip_duration_seconds: 90,
    captions_enabled: true,
    status: "completed",
    progress: 100,
    error_message: null,
    created_at: FUTURE,
    completed_at: FUTURE,
    expires_at: EXPIRY,
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
    ...overrides,
  };
}

function clipObjectsMap(...clipIds: string[]): Map<string, { name: string; size: number }> {
  const map = new Map<string, { name: string; size: number }>();
  for (const id of clipIds) map.set(`${id}.mp4`, { name: `${id}.mp4`, size: 1234 });
  return map;
}

beforeEach(() => {
  vi.clearAllMocks();
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
  findJobMock.mockResolvedValue(null);
  listClipsMock.mockResolvedValue([]);
  findClipMock.mockResolvedValue(null);
  listObjectsMock.mockResolvedValue(clipObjectsMap());
  // Echo the requested TTL back (like the real implementation).
  signMock.mockImplementation(async (_ref: unknown, expiresIn: number) => ({
    signedUrl: "https://supabase.test/signed",
    expiresInSeconds: expiresIn,
  }));
});

afterEach(() => {
  vi.unstubAllEnvs();
});

// -- GET /api/v1/jobs/[jobId]/results ------------------------------------------------

describe("GET /api/v1/jobs/[jobId]/results", () => {
  it("requires a session (401) and never touches the job table", async () => {
    const response = await GET_RESULTS(new Request(`http://localhost/api/v1/jobs/${JOB_ID}/results`), {
      params: { jobId: JOB_ID },
    });
    expect(response.status).toBe(401);
    expect(findJobMock).not.toHaveBeenCalled();
  });

  it("rejects non-UUID job ids without a session or DB round trip (anti-enumeration)", async () => {
    const response = await GET_RESULTS(requestWithSession("/api/v1/jobs/not-a-uuid/results"), {
      params: { jobId: "not-a-uuid" },
    });
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Job not found." });
    expect(findSessionMock).not.toHaveBeenCalled();
    expect(findJobMock).not.toHaveBeenCalled();
  });

  it("returns 404 for a job owned by another session (indistinguishable from missing)", async () => {
    findJobMock.mockResolvedValue(null); // session-scoped query finds nothing
    const response = await GET_RESULTS(requestWithSession(`/api/v1/jobs/${JOB_ID}/results`), {
      params: { jobId: JOB_ID },
    });
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Job not found." });
  });

  it("returns only safe metadata for the job's clips — no storage paths, no ownership", async () => {
    findJobMock.mockResolvedValue(jobRow());
    listClipsMock.mockResolvedValue([
      clipRow(), // ready + canonical path → downloadable
      clipRow({
        id: CLIP_ID_2,
        title: "Broken path clip",
        sort_order: 1,
        storage_path: `clips/${OTHER_JOB_ID}/${CLIP_ID_2}.mp4`, // foreign job → NOT downloadable
      }),
      clipRow({
        id: CLIP_ID_2,
        title: "Still rendering",
        sort_order: 2,
        status: "processing",
      }),
    ]);
    listObjectsMock.mockResolvedValue(clipObjectsMap(CLIP_ID)); // 1234 bytes

    const response = await GET_RESULTS(requestWithSession(`/api/v1/jobs/${JOB_ID}/results`), {
      params: { jobId: JOB_ID },
    });
    expect(response.status).toBe(200);

    const body = (await response.json()) as {
      job: Record<string, unknown>;
      clips: Array<Record<string, unknown>>;
      zip: Record<string, unknown>;
    };
    // The clip listing is job-scoped — the server-verified id only.
    expect(listClipsMock).toHaveBeenCalledWith(JOB_ID);

    expect(body.job).toEqual({
      id: JOB_ID,
      status: "completed",
      progress: 100,
      createdAt: FUTURE,
      completedAt: FUTURE,
      expiresAt: EXPIRY,
      // Two clips have status `ready` (the broken-path one is ready but not downloadable).
      clipsReady: 2,
    });
    expect(body.clips).toEqual([
      { id: CLIP_ID, title: "The opening hook", duration: 60, sortOrder: 0, status: "ready", downloadable: true },
      { id: CLIP_ID_2, title: "Broken path clip", duration: 60, sortOrder: 1, status: "ready", downloadable: false },
      { id: CLIP_ID_2, title: "Still rendering", duration: 60, sortOrder: 2, status: "processing", downloadable: false },
    ]);
    // ZIP feasibility: only the ONE downloadable clip counts toward the size.
    expect(body.zip).toEqual({ available: true, totalBytes: 1234, limitBytes: 1024 * 1024 * 1024 });

    // Internal paths and ownership internals never leave the server.
    const wire = JSON.stringify(body);
    expect(wire).not.toContain("clips/");
    expect(wire).not.toContain("sources/");
    expect(wire).not.toContain("storage_path");
    expect(wire).not.toContain(SESSION_ID);
  });

  it("reports 0 ready clips for a completed job with nothing rendered", async () => {
    findJobMock.mockResolvedValue(jobRow());
    listClipsMock.mockResolvedValue([clipRow({ status: "failed" })]);
    const response = await GET_RESULTS(requestWithSession(`/api/v1/jobs/${JOB_ID}/results`), {
      params: { jobId: JOB_ID },
    });
    const body = (await response.json()) as { job: { clipsReady: number }; zip: { available: boolean; totalBytes: number } };
    expect(response.status).toBe(200);
    expect(body.job.clipsReady).toBe(0);
    expect(body.zip).toEqual(expect.objectContaining({ available: false, totalBytes: 0 }));
  });

  it("computes zip feasibility from object sizes and the configured cap", async () => {
    findJobMock.mockResolvedValue(jobRow());
    listClipsMock.mockResolvedValue([
      clipRow(),
      clipRow({ id: CLIP_ID_2, title: "Two", sort_order: 1, storage_path: `clips/${JOB_ID}/${CLIP_ID_2}.mp4` }),
    ]);
    listObjectsMock.mockResolvedValue(new Map([
      [`${CLIP_ID}.mp4`, { name: `${CLIP_ID}.mp4`, size: 600 }],
      [`${CLIP_ID_2}.mp4`, { name: `${CLIP_ID_2}.mp4`, size: 600 }],
    ]));

    // Default cap (1 GiB): available.
    let response = await GET_RESULTS(requestWithSession(`/api/v1/jobs/${JOB_ID}/results`), {
      params: { jobId: JOB_ID },
    });
    let body = (await response.json()) as { zip: { available: boolean; totalBytes: number } };
    expect(body.zip).toEqual({ available: true, totalBytes: 1200, limitBytes: 1024 * 1024 * 1024 });

    // Cap below the total: unavailable (the UI explains the per-clip fallback).
    vi.stubEnv("MAX_ZIP_SIZE_BYTES", "1000");
    response = await GET_RESULTS(requestWithSession(`/api/v1/jobs/${JOB_ID}/results`), {
      params: { jobId: JOB_ID },
    });
    body = (await response.json()) as { zip: { available: boolean; totalBytes: number; limitBytes: number } };
    expect(body.zip).toEqual({ available: false, totalBytes: 1200, limitBytes: 1000 });
  });

  it("omits the zip block while the job is still processing", async () => {
    findJobMock.mockResolvedValue(jobRow({ status: "rendering" }));
    listClipsMock.mockResolvedValue([clipRow({ status: "processing" })]);
    const response = await GET_RESULTS(requestWithSession(`/api/v1/jobs/${JOB_ID}/results`), {
      params: { jobId: JOB_ID },
    });
    const body = (await response.json()) as { zip?: unknown };
    expect(response.status).toBe(200);
    expect(body.zip).toBeUndefined();
    expect(listObjectsMock).not.toHaveBeenCalled(); // no storage round trip while processing
  });
});

// -- GET .../clips/[clipId]/preview ----------------------------------------------------

describe("GET /api/v1/jobs/[jobId]/clips/[clipId]/preview", () => {
  const clipParams = { params: { jobId: JOB_ID, clipId: CLIP_ID } };

  it("requires a session (401)", async () => {
    const response = await GET_PREVIEW(new Request(`http://localhost/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/preview`), clipParams);
    expect(response.status).toBe(401);
    expect(signMock).not.toHaveBeenCalled();
  });

  it("rejects non-UUID clip ids before any session/DB round trip (anti-enumeration)", async () => {
    const response = await GET_PREVIEW(requestWithSession("/api/v1/jobs/x/clips/bad-id/preview"), {
      params: { jobId: JOB_ID, clipId: "bad-id" },
    });
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Clip not found." });
    expect(findSessionMock).not.toHaveBeenCalled();
    expect(findJobMock).not.toHaveBeenCalled();
  });

  it("returns the generic clip 404 for a foreign or missing job", async () => {
    findJobMock.mockResolvedValue(null);
    const response = await GET_PREVIEW(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/preview`), clipParams);
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Clip not found." });
    expect(signMock).not.toHaveBeenCalled();
  });

  it("denies a clip that does not belong to the job (job-scoped lookup → 404)", async () => {
    findJobMock.mockResolvedValue(jobRow());
    findClipMock.mockResolvedValue(null); // the query is .eq(job_id).eq(id) — nothing comes back
    const response = await GET_PREVIEW(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/preview`), clipParams);
    expect(response.status).toBe(404);
    expect(findClipMock).toHaveBeenCalledWith(JOB_ID, CLIP_ID);
    expect(signMock).not.toHaveBeenCalled();
  });

  it("does not mint a URL for a clip that is not ready (404, same body)", async () => {
    findJobMock.mockResolvedValue(jobRow());
    findClipMock.mockResolvedValue(clipRow({ status: "processing" }));
    const response = await GET_PREVIEW(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/preview`), clipParams);
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Clip not found." });
    expect(signMock).not.toHaveBeenCalled();
  });

  it("does not mint a URL when the stored path points at a different job (404, same body)", async () => {
    findJobMock.mockResolvedValue(jobRow());
    findClipMock.mockResolvedValue(clipRow({ storage_path: `clips/${OTHER_JOB_ID}/${CLIP_ID}.mp4` }));
    const response = await GET_PREVIEW(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/preview`), clipParams);
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Clip not found." });
    expect(signMock).not.toHaveBeenCalled();
  });

  it("does not mint a URL when the object is missing from Storage (404, same body)", async () => {
    findJobMock.mockResolvedValue(jobRow());
    findClipMock.mockResolvedValue(clipRow());
    listObjectsMock.mockResolvedValue(clipObjectsMap()); // empty: nothing exists
    const response = await GET_PREVIEW(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/preview`), clipParams);
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Clip not found." });
    expect(signMock).not.toHaveBeenCalled();
  });

  it("mints a SHORT-LIVED preview URL (300s, inline) for a verified clip", async () => {
    findJobMock.mockResolvedValue(jobRow());
    findClipMock.mockResolvedValue(clipRow());
    listObjectsMock.mockResolvedValue(clipObjectsMap(CLIP_ID));
    const response = await GET_PREVIEW(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/preview`), clipParams);
    expect(response.status).toBe(200);
    const body = (await response.json()) as { url: string; expiresInSeconds: number };
    expect(body).toEqual({ url: "https://supabase.test/signed", expiresInSeconds: 300 });
    expect(listObjectsMock).toHaveBeenCalledWith(JOB_ID);
    expect(signMock).toHaveBeenCalledTimes(1);
    // No `download` option → inline playback for the <video> element.
    expect(signMock).toHaveBeenCalledWith({ jobId: JOB_ID, clipId: CLIP_ID }, 300);
  });

  it("honours the configured preview URL lifetime", async () => {
    vi.stubEnv("CLIP_PREVIEW_URL_SECONDS", "60");
    findJobMock.mockResolvedValue(jobRow());
    findClipMock.mockResolvedValue(clipRow());
    listObjectsMock.mockResolvedValue(clipObjectsMap(CLIP_ID));
    await GET_PREVIEW(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/preview`), clipParams);
    expect(signMock).toHaveBeenCalledWith({ jobId: JOB_ID, clipId: CLIP_ID }, 60);
  });
});

// -- GET .../clips/[clipId]/download ----------------------------------------------------

describe("GET /api/v1/jobs/[jobId]/clips/[clipId]/download", () => {
  const clipParams = { params: { jobId: JOB_ID, clipId: CLIP_ID } };

  it("mints a DOWNLOAD URL (900s, attachment) with a sanitized filename", async () => {
    findJobMock.mockResolvedValue(jobRow());
    findClipMock.mockResolvedValue(clipRow());
    listObjectsMock.mockResolvedValue(clipObjectsMap(CLIP_ID));
    const response = await GET_DOWNLOAD(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/download`), clipParams);
    expect(response.status).toBe(200);
    const body = (await response.json()) as { url: string; expiresInSeconds: number };
    expect(body.expiresInSeconds).toBe(900);
    expect(signMock).toHaveBeenCalledTimes(1);
    expect(signMock).toHaveBeenCalledWith(
      { jobId: JOB_ID, clipId: CLIP_ID },
      900,
      { download: true, fileName: "The opening hook.mp4" }
    );
  });

  it("never lets a hostile title escape a single filename segment", async () => {
    const hostile = `../../etc/passwd:\u0000*?"<>|`;
    findJobMock.mockResolvedValue(jobRow());
    findClipMock.mockResolvedValue(clipRow({ title: hostile }));
    listObjectsMock.mockResolvedValue(clipObjectsMap(CLIP_ID));
    const response = await GET_DOWNLOAD(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/download`), clipParams);
    expect(response.status).toBe(200);

    const fileName = signMock.mock.calls[0]?.[2] as { fileName: string };
    expect(fileName.fileName).toBe(".. .. etc passwd.mp4");
    // Single safe segment: no separators, no control characters.
    expect(fileName.fileName).not.toMatch(/[\\/\u0000-\u001f]/);
  });

  it("falls back to a default name when the title sanitizes to nothing", async () => {
    findJobMock.mockResolvedValue(jobRow());
    findClipMock.mockResolvedValue(clipRow({ title: `\u0000\u0000:://**` }));
    listObjectsMock.mockResolvedValue(clipObjectsMap(CLIP_ID));
    await GET_DOWNLOAD(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/download`), clipParams);
    const fileName = signMock.mock.calls[0]?.[2] as { fileName: string };
    expect(fileName.fileName).toBe("clip.mp4");
  });

  it("denies downloads for clips of a job the session does not own (404)", async () => {
    findJobMock.mockResolvedValue(null); // cross-user: session-scoped lookup finds nothing
    const response = await GET_DOWNLOAD(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/download`), clipParams);
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Clip not found." });
    expect(signMock).not.toHaveBeenCalled();
  });

  it("denies a clip with a missing Storage object (404, no URL)", async () => {
    findJobMock.mockResolvedValue(jobRow());
    findClipMock.mockResolvedValue(clipRow());
    listObjectsMock.mockResolvedValue(clipObjectsMap()); // CLIP_ID absent
    const response = await GET_DOWNLOAD(requestWithSession(`/api/v1/jobs/${JOB_ID}/clips/${CLIP_ID}/download`), clipParams);
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Clip not found." });
    expect(signMock).not.toHaveBeenCalled();
  });
});
