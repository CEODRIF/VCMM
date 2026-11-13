import { beforeEach, describe, expect, it, vi } from "vitest";

// TASK 011: provider capability gate + Twitch support at the API boundary.
// The provider modules are stubbed with configurable capability flags; the
// route, payload validation, and session logic stay real. NO live provider
// APIs are called and NO real credentials exist anywhere in this file.

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

const youtubeState = { isAvailable: false, supportsMaterialization: false };
const twitchState = { isAvailable: false, supportsMaterialization: false };

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

import { POST as POST_JOBS } from "@/app/api/v1/jobs/route";
import { findSessionByTokenHash } from "@/lib/db/sessions";
import { insertJob } from "@/lib/db/jobs";
import { validateTwitchUrl } from "@/lib/validation/clips";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const insertMock = insertJob as ReturnType<typeof vi.fn>;

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

function requestWithoutSession(body: unknown): Request {
  return new Request("http://localhost/api/v1/jobs", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

const clipFields = { clipCount: 3, clipDurationSeconds: 120, captionsEnabled: true };

beforeEach(() => {
  vi.clearAllMocks();
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
  insertMock.mockResolvedValue({ id: JOB_ID });
  // honest defaults: nothing configured on this deployment
  Object.assign(youtubeState, { isAvailable: false, supportsMaterialization: false });
  Object.assign(twitchState, { isAvailable: false, supportsMaterialization: false });
});

describe("POST /api/v1/jobs — Twitch provider (TASK 011)", () => {
  it("refuses Twitch jobs with a clear 503 when the provider is unconfigured", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "twitch",
        twitchUrl: "https://twitch.tv/videos/123456789",
        ...clipFields,
      })
    );
    expect(response.status).toBe(503);
    const body = (await response.json()) as { error?: string };
    expect(body.error).toMatch(/twitch source provider is not configured/i);
    // no job row may be created for an unavailable provider
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("refuses with a DISTINCT 503 when metadata is available but materialization is unsupported", async () => {
    Object.assign(twitchState, { isAvailable: true, supportsMaterialization: false });
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "twitch",
        twitchUrl: "https://twitch.tv/videos/123456789",
        ...clipFields,
      })
    );
    expect(response.status).toBe(503);
    const body = (await response.json()) as { error?: string };
    expect(body.error).toMatch(/cannot be materialized/i);
    expect(body.error).toMatch(/direct video file upload/i);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("accepts canonical Twitch VOD URL forms (reaches the provider gate, not validation)", async () => {
    for (const twitchUrl of [
      "https://twitch.tv/videos/123456789",
      "https://twitch.tv/somechannel/videos/123456789",
      "https://www.twitch.tv/videos/42",
      "https://m.twitch.tv/a_b_c/videos/7",
    ]) {
      const response = await POST_JOBS(
        requestWithSession({ sourceType: "twitch", twitchUrl, ...clipFields })
      );
      // 503 = passed URL validation, refused at the capability gate
      expect(response.status, twitchUrl).toBe(503);
      expect(insertMock).not.toHaveBeenCalled();
    }
  });

  it.each([
    ["https://not-twitch.tv/videos/123456789", "lookalike domain"],
    ["https://twitch.tv.evil.example/videos/123456789", "suffix lookalike"],
    ["https://evil-twitch.tv/videos/123456789", "prefix lookalike"],
    ["https://twitch.tv/live/somechannel", "live URL is not a VOD"],
    ["https://twitch.tv/somechannel", "bare channel is not a VOD"],
    ["https://twitch.tv/directory/browse", "directory page is not a VOD"],
    ["https://twitch.tv/videos/", "missing vod id"],
    ["https://twitch.tv/videos/12a34", "non-numeric vod id"],
    ["ftp://twitch.tv/videos/123", "non-http scheme"],
    ["https://youtube.com/watch?v=x", "wrong platform"],
    ["not a url", "not a URL"],
  ])("rejects unsupported Twitch URL forms (%s) with 400", async (twitchUrl) => {
    const response = await POST_JOBS(
      requestWithSession({ sourceType: "twitch", twitchUrl, ...clipFields })
    );
    expect(response.status).toBe(400);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("requires twitchUrl for twitch sourceType (400 without it)", async () => {
    const response = await POST_JOBS(requestWithSession({ sourceType: "twitch", ...clipFields }));
    expect(response.status).toBe(400);
    expect(insertMock).not.toHaveBeenCalled();
  });
});

describe("POST /api/v1/jobs — YouTube compatibility (TASK 011)", () => {
  it("keeps the unconfigured 503 behavior for YouTube jobs", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "youtube",
        youtubeUrl: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ...clipFields,
      })
    );
    expect(response.status).toBe(503);
    const body = (await response.json()) as { error?: string };
    expect(body.error).toMatch(/youtube source provider is not configured/i);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("gates YouTube on supportsMaterialization too (distinct 503)", async () => {
    Object.assign(youtubeState, { isAvailable: true, supportsMaterialization: false });
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "youtube",
        youtubeUrl: "https://youtu.be/dQw4w9WgXcQ",
        ...clipFields,
      })
    );
    expect(response.status).toBe(503);
    const body = (await response.json()) as { error?: string };
    expect(body.error).toMatch(/cannot be materialized/i);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("still rejects non-YouTube URLs with 400", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "youtube",
        youtubeUrl: "https://example.com/video",
        ...clipFields,
      })
    );
    expect(response.status).toBe(400);
    expect(insertMock).not.toHaveBeenCalled();
  });
});

describe("POST /api/v1/jobs — authorization & duplicate protection", () => {
  it("never creates provider jobs without a verified session (401)", async () => {
    const twitch = await POST_JOBS(
      requestWithoutSession({ sourceType: "twitch", twitchUrl: "https://twitch.tv/videos/1", ...clipFields })
    );
    expect(twitch.status).toBe(401);
    const youtube = await POST_JOBS(
      requestWithoutSession({ sourceType: "youtube", youtubeUrl: "https://youtu.be/x", ...clipFields })
    );
    expect(youtube.status).toBe(401);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("duplicate provider submissions cannot create jobs (gate is stateless)", async () => {
    const payload = { sourceType: "twitch", twitchUrl: "https://twitch.tv/videos/123456789", ...clipFields };
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const response = await POST_JOBS(requestWithSession(payload));
      expect(response.status).toBe(503);
    }
    // every refusal leaves zero rows — there is nothing to deduplicate
    expect(insertMock).not.toHaveBeenCalled();
  });
});

describe("POST /api/v1/jobs — safe error serialization", () => {
  it("provider errors serialize as a single safe message (no internals)", async () => {
    const response = await POST_JOBS(
      requestWithSession({ sourceType: "twitch", twitchUrl: "https://twitch.tv/videos/1", ...clipFields })
    );
    expect(response.status).toBe(503);
    const body = (await response.json()) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["error"]);
    expect(typeof body.error).toBe("string");
    // no provider configuration, tokens, or internal URLs in the payload
    const text = JSON.stringify(body);
    expect(text).not.toMatch(/client[_-]?secret|access[_-]?token|bearer /i);
    expect(text).not.toContain("clipforge-sources");
  });
});

describe("POST /api/v1/jobs — direct upload regression", () => {
  it("upload jobs still create rows (201) and are unaffected by provider gates", async () => {
    const response = await POST_JOBS(
      requestWithSession({ sourceType: "upload", fileName: "my video.mp4", ...clipFields })
    );
    expect(response.status).toBe(201);
    const body = (await response.json()) as { job?: { id: string; status: string } };
    // The service generates a server-side UUID (the mock's id is ignored).
    expect(body.job?.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
    expect(body.job?.status).toBe("uploading");
    expect(insertMock).toHaveBeenCalledTimes(1);
    const call = insertMock.mock.calls[0];
    expect(call).toBeDefined();
    expect(call?.[0]).toMatchObject({
      sessionId: SESSION_ID,
      sourceType: "upload",
      initialStatus: "uploading",
    });
  });
});

describe("validateTwitchUrl (client-side mirror)", () => {
  it("accepts canonical VOD forms", () => {
    for (const url of [
      "https://twitch.tv/videos/123456789",
      "https://www.twitch.tv/chan/videos/1",
      "https://m.twitch.tv/a_b/videos/99",
    ]) {
      expect(validateTwitchUrl(url).valid, url).toBe(true);
    }
  });

  it("rejects empty, malformed, lookalike, and non-VOD forms", () => {
    for (const url of [
      "",
      "https://twitch.tv/live/chan",
      "https://twitch.tv/chan",
      "https://not-twitch.tv/videos/1",
      "https://twitch.tv.evil.example/videos/1",
      "https://twitch.tv/videos/abc",
      "not a url",
    ]) {
      const result = validateTwitchUrl(url);
      expect(result.valid, url).toBe(false);
      expect(result.error, url).toBeTruthy();
    }
  });
});
