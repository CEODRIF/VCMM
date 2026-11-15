/**
 * TASK 017 — duplicate-submission guard (R2).
 *
 * A double-click or two tabs must not enqueue the same source twice. While
 * the first job for a source URL is still ACTIVE, the second creation
 * answers 409 (clear, actionable) and creates NO job row. Once the first
 * job is terminal (completed/failed), the source may be re-submitted —
 * findActiveJobForSourceUrl only matches non-terminal statuses, so it
 * returns null and creation proceeds.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/db/sessions", () => ({
  findSessionByTokenHash: vi.fn(),
  createSessionRow: vi.fn(),
  deleteSessionRow: vi.fn(),
}));
vi.mock("@/lib/db/jobs", () => ({
  insertJob: vi.fn(),
  findActiveJobForSourceUrl: vi.fn(),
  findJobForSession: vi.fn(),
  listJobsForSession: vi.fn(),
  setUploadAuthorization: vi.fn(),
  markUploadComplete: vi.fn(),
}));

const twitchState = { isAvailable: false, supportsMaterialization: false };
vi.mock("@/lib/providers/twitch", () => ({
  getTwitchProvider: () => ({
    name: "stub",
    isAvailable: () => twitchState.isAvailable,
    supportsMaterialization: () => twitchState.supportsMaterialization,
    fetchSource: async () => {
      throw new Error("stub: no live integration");
    },
  }),
}));
const youtubeState = { isAvailable: false, supportsMaterialization: false };
vi.mock("@/lib/providers/youtube", () => ({
  getYouTubeProvider: () => ({
    name: "stub",
    isAvailable: () => youtubeState.isAvailable,
    supportsMaterialization: () => youtubeState.supportsMaterialization,
    fetchSource: async () => {
      throw new Error("stub: no live integration");
    },
  }),
}));

import { POST } from "@/app/api/v1/jobs/route";
import { findSessionByTokenHash } from "@/lib/db/sessions";
import { findActiveJobForSourceUrl, insertJob } from "@/lib/db/jobs";
import { jobCreationRateLimiter } from "@/lib/security/rate-limit";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const findActiveMock = findActiveJobForSourceUrl as ReturnType<typeof vi.fn>;
const insertMock = insertJob as ReturnType<typeof vi.fn>;

const SESSION_TOKEN = "b".repeat(64);
const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const FUTURE = new Date(Date.now() + 3600_000).toISOString();
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const CLIP_URL = "https://clips.twitch.tv/RewolvedKleinerPinguin";
const DIRECT_URL = "https://cdn.example.com/video/lecture.mp4";
const clipFields = { clipCount: 3, clipDurationSeconds: 120, captionsEnabled: true };

function request(body: unknown): Request {
  return new Request("http://localhost/api/v1/jobs", {
    method: "POST",
    headers: {
      cookie: `clipforge_session=${SESSION_TOKEN}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  jobCreationRateLimiter.resetAll();
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
  insertMock.mockResolvedValue({ id: JOB_ID });
  findActiveMock.mockResolvedValue(null);
});

describe("twitch clip duplicate submission", () => {
  it("answers 409 while an active job exists for the same clip — no second row", async () => {
    findActiveMock.mockResolvedValue({ id: JOB_ID });
    const response = await POST(request({ sourceType: "twitch", videoUrl: CLIP_URL, ...clipFields }));
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(409);
    expect(body.error).toMatch(/already have an active job/i);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("allows creation when no active job exists for the clip (201)", async () => {
    findActiveMock.mockResolvedValue(null);
    const response = await POST(request({ sourceType: "twitch", videoUrl: CLIP_URL, ...clipFields }));
    expect(response.status).toBe(201);
    expect(findActiveMock).toHaveBeenCalledWith(SESSION_ID, CLIP_URL);
    expect(insertMock).toHaveBeenCalledTimes(1);
  });

  it("allows re-submission after the first job reached a terminal state (201)", async () => {
    // The DB query only matches ACTIVE statuses — a completed/failed prior
    // job yields null here, which is exactly the re-submission case.
    findActiveMock.mockResolvedValue(null);
    const response = await POST(request({ sourceType: "twitch", videoUrl: CLIP_URL, ...clipFields }));
    expect(response.status).toBe(201);
  });

  it("does not block a DIFFERENT clip URL while one clip is active (201)", async () => {
    findActiveMock.mockResolvedValue(null); // no active job for THIS url
    const other = "https://clips.twitch.tv/AnotherClip";
    const response = await POST(request({ sourceType: "twitch", videoUrl: other, ...clipFields }));
    expect(response.status).toBe(201);
    expect(findActiveMock).toHaveBeenCalledWith(SESSION_ID, other);
  });
});

describe("direct URL duplicate submission", () => {
  it("answers 409 while an active job exists for the same direct URL", async () => {
    findActiveMock.mockResolvedValue({ id: JOB_ID });
    const response = await POST(
      request({ sourceType: "direct_url", videoUrl: DIRECT_URL, ...clipFields })
    );
    expect(response.status).toBe(409);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("allows creation when no active job exists (201)", async () => {
    findActiveMock.mockResolvedValue(null);
    const response = await POST(
      request({ sourceType: "direct_url", videoUrl: DIRECT_URL, ...clipFields })
    );
    expect(response.status).toBe(201);
    expect(findActiveMock).toHaveBeenCalledWith(SESSION_ID, DIRECT_URL);
    expect(insertMock).toHaveBeenCalledTimes(1);
  });
});

describe("upload jobs are unaffected by the URL guard", () => {
  it("creates an upload job without consulting the source-URL guard (201)", async () => {
    const response = await POST(
      request({ sourceType: "upload", fileName: "lecture.mp4", ...clipFields })
    );
    expect(response.status).toBe(201);
    expect(findActiveMock).not.toHaveBeenCalled();
    expect(insertMock).toHaveBeenCalledTimes(1);
  });
});
