import { beforeEach, describe, expect, it, vi } from "vitest";

// TASK 015 P3 — generation modes + explicit output FPS at the API boundary.
// The db/provider modules are mocked (no network, no database); the route,
// payload validation (zod), and the persisted job settings (insertJob
// arguments) stay REAL. The GET route maps the new job columns to the
// public payload the processing panel displays.

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

vi.mock("@/lib/providers/youtube", () => ({
  getYouTubeProvider: () => ({
    name: "stub",
    isAvailable: () => false,
    supportsMaterialization: () => false,
    fetchSource: async () => {
      throw new Error("stub: no live integration");
    },
  }),
}));
vi.mock("@/lib/providers/twitch", () => ({
  getTwitchProvider: () => ({
    name: "stub",
    isAvailable: () => false,
    supportsMaterialization: () => false,
    fetchSource: async () => {
      throw new Error("stub: no live integration");
    },
  }),
}));

import { GET as GET_JOB } from "@/app/api/v1/jobs/[jobId]/route";
import { POST as POST_JOBS } from "@/app/api/v1/jobs/route";
import { countClipsForJob, findJobForSession, insertJob } from "@/lib/db/jobs";
import { findSessionByTokenHash } from "@/lib/db/sessions";
import { jobCreationRateLimiter } from "@/lib/security/rate-limit";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const insertMock = insertJob as ReturnType<typeof vi.fn>;
const findJobMock = findJobForSession as ReturnType<typeof vi.fn>;
const countClipsMock = countClipsForJob as ReturnType<typeof vi.fn>;

const SESSION_TOKEN = "b".repeat(64);
const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const FUTURE = new Date(Date.now() + 3600_000).toISOString();
const JOB_ID = "22222222-2222-4222-8222-222222222222";

function requestWithSession(body: unknown): Request {
  return new Request("http://localhost/api/v1/jobs", {
    method: "POST",
    headers: {
      cookie: `clipforge_session=${SESSION_TOKEN}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  });
}

function getRequest(jobId: string): Request {
  return new Request(`http://localhost/api/v1/jobs/${jobId}`, {
    headers: { cookie: `clipforge_session=${SESSION_TOKEN}` },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  jobCreationRateLimiter.resetAll();
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
  insertMock.mockResolvedValue({ id: JOB_ID });
  countClipsMock.mockResolvedValue(0);
});

describe("POST /api/v1/jobs — generation mode (TASK 015 P3)", () => {
  it("full_coverage WITHOUT clipCount is accepted and persists requestedClips NULL", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "upload",
        fileName: "generated.mp4",
        clipDurationSeconds: 120,
        generationMode: "full_coverage",
        outputFps: 60,
        captionsEnabled: true,
      })
    );
    expect(response.status).toBe(201);
    expect(insertMock).toHaveBeenCalledWith(
      expect.objectContaining({
        sourceType: "upload",
        requestedClips: null, // derivation slot — never truncates coverage
        generationMode: "full_coverage",
        outputFps: 60,
        clipDurationSeconds: 120,
      })
    );
  });

  it("full_coverage WITH a clipCount still persists NULL (the count is ignored, never a truncation)", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "upload",
        fileName: "generated.mp4",
        clipCount: 3,
        clipDurationSeconds: 120,
        generationMode: "full_coverage",
        captionsEnabled: true,
      })
    );
    expect(response.status).toBe(201);
    expect(insertMock).toHaveBeenCalledWith(
      expect.objectContaining({ requestedClips: null, generationMode: "full_coverage" })
    );
  });

  it("highlights WITHOUT clipCount is rejected with 400 (no job row)", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "upload",
        fileName: "generated.mp4",
        clipDurationSeconds: 120,
        generationMode: "highlights",
        captionsEnabled: true,
      })
    );
    expect(response.status).toBe(400);
    const body = (await response.json()) as { error?: string };
    expect(body.error).toMatch(/clipCount/i);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("full_coverage on a direct_url source is accepted without clipCount", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "direct_url",
        videoUrl: "https://cdn.example.com/streams/vod.mp4",
        clipDurationSeconds: 60,
        generationMode: "full_coverage",
        outputFps: 30,
        captionsEnabled: false,
      })
    );
    expect(response.status).toBe(201);
    expect(insertMock).toHaveBeenCalledWith(
      expect.objectContaining({
        sourceType: "direct_url",
        requestedClips: null,
        generationMode: "full_coverage",
        outputFps: 30,
      })
    );
  });

  it("an unknown generation mode is rejected with 400", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "upload",
        fileName: "generated.mp4",
        clipCount: 3,
        clipDurationSeconds: 120,
        generationMode: "bogus",
      })
    );
    expect(response.status).toBe(400);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("legacy payloads (no generationMode) still default to highlights", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "upload",
        fileName: "generated.mp4",
        clipCount: 3,
        clipDurationSeconds: 120,
        captionsEnabled: true,
      })
    );
    expect(response.status).toBe(201);
    expect(insertMock).toHaveBeenCalledWith(
      expect.objectContaining({
        generationMode: "highlights",
        outputFps: null, // legacy source-rate-preserving render
        requestedClips: 3,
      })
    );
  });
});

describe("POST /api/v1/jobs — output frame rate (TASK 015 P3)", () => {
  const base = {
    sourceType: "upload",
    fileName: "generated.mp4",
    clipCount: 3,
    clipDurationSeconds: 120,
    captionsEnabled: true,
  };

  it("fps 30 is accepted and persisted", async () => {
    const response = await POST_JOBS(requestWithSession({ ...base, outputFps: 30 }));
    expect(response.status).toBe(201);
    expect(insertMock).toHaveBeenCalledWith(expect.objectContaining({ outputFps: 30 }));
  });

  it("fps 60 is accepted and persisted", async () => {
    const response = await POST_JOBS(requestWithSession({ ...base, outputFps: 60 }));
    expect(response.status).toBe(201);
    expect(insertMock).toHaveBeenCalledWith(expect.objectContaining({ outputFps: 60 }));
  });

  it("fps 24 is rejected with 400 (never a silent downscale)", async () => {
    const response = await POST_JOBS(requestWithSession({ ...base, outputFps: 24 }));
    expect(response.status).toBe(400);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("fps 120 is rejected with 400", async () => {
    const response = await POST_JOBS(requestWithSession({ ...base, outputFps: 120 }));
    expect(response.status).toBe(400);
    expect(insertMock).not.toHaveBeenCalled();
  });
});

describe("POST /api/v1/jobs — duration + count boundaries (TASK 015 P3)", () => {
  const base = {
    sourceType: "upload",
    fileName: "generated.mp4",
    clipCount: 3,
    clipDurationSeconds: 120,
  };

  it("clip duration below the product band (59s) is rejected", async () => {
    const response = await POST_JOBS(requestWithSession({ ...base, clipDurationSeconds: 59 }));
    expect(response.status).toBe(400);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("clip duration above the product band (181s) is rejected", async () => {
    const response = await POST_JOBS(requestWithSession({ ...base, clipDurationSeconds: 181 }));
    expect(response.status).toBe(400);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("clip count 0 and 11 are rejected; 1 and 10 are accepted (highlights band)", async () => {
    for (const bad of [0, 11]) {
      const response = await POST_JOBS(requestWithSession({ ...base, clipCount: bad }));
      expect(response.status).toBe(400);
    }
    expect(insertMock).not.toHaveBeenCalled();
    const low = await POST_JOBS(requestWithSession({ ...base, clipCount: 1 }));
    expect(low.status).toBe(201);
    expect(insertMock).toHaveBeenLastCalledWith(expect.objectContaining({ requestedClips: 1 }));
    const high = await POST_JOBS(requestWithSession({ ...base, clipCount: 10 }));
    expect(high.status).toBe(201);
    expect(insertMock).toHaveBeenLastCalledWith(expect.objectContaining({ requestedClips: 10 }));
  });
});

describe("GET /api/v1/jobs/[jobId] — public job mapping (TASK 015 P3)", () => {
  it("exposes generation mode, fps, and the measured source duration", async () => {
    findJobMock.mockResolvedValue({
      id: JOB_ID,
      source_type: "upload",
      requested_clips: null,
      clip_duration_seconds: 120,
      captions_enabled: true,
      generation_mode: "full_coverage",
      output_fps: 60,
      status: "analyzing",
      progress: 20,
      created_at: "2026-11-15T00:00:00.000Z",
      completed_at: null,
      upload_completed_at: "2026-11-15T00:00:05.000Z",
      error_message: null,
      last_error: null,
      source_metadata: { duration_seconds: 3645 },
    });
    const response = await GET_JOB(getRequest(JOB_ID), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(200);
    const body = (await response.json()) as { job?: Record<string, unknown> };
    expect(body.job).toMatchObject({
      requestedClips: null,
      generationMode: "full_coverage",
      outputFps: 60,
      sourceDurationSeconds: 3645,
      status: "analyzing",
      clipsCount: 0,
    });
  });

  it("reports null source duration until the probe has persisted one", async () => {
    findJobMock.mockResolvedValue({
      id: JOB_ID,
      source_type: "upload",
      requested_clips: 3,
      clip_duration_seconds: 120,
      captions_enabled: true,
      generation_mode: "highlights",
      output_fps: 30,
      status: "queued",
      progress: 0,
      created_at: "2026-11-15T00:00:00.000Z",
      completed_at: null,
      upload_completed_at: null,
      error_message: null,
      last_error: null,
      source_metadata: null,
    });
    const response = await GET_JOB(getRequest(JOB_ID), { params: { jobId: JOB_ID } });
    expect(response.status).toBe(200);
    const body = (await response.json()) as { job?: Record<string, unknown> };
    expect(body.job).toMatchObject({
      requestedClips: 3,
      generationMode: "highlights",
      outputFps: 30,
      sourceDurationSeconds: null,
    });
  });
});
