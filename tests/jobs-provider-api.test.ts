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
import { jobCreationRateLimiter } from "@/lib/security/rate-limit";
import { classifyVideoUrl, validateTwitchUrl } from "@/lib/validation/clips";

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
  // TASK 012: the route now rate-limits job creation per session — reset the
  // shared limiter so each test starts with a clean window (deterministic).
  jobCreationRateLimiter.resetAll();
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
  insertMock.mockResolvedValue({ id: JOB_ID });
  // honest defaults: nothing configured on this deployment
  Object.assign(youtubeState, { isAvailable: false, supportsMaterialization: false });
  Object.assign(twitchState, { isAvailable: false, supportsMaterialization: false });
});

describe("POST /api/v1/jobs — Twitch provider (TASK 011 → TASK 014 per-class gates)", () => {
  it("refuses Twitch VOD jobs with an actionable 503 (no official VOD media endpoint)", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "twitch",
        twitchUrl: "https://twitch.tv/videos/123456789",
        ...clipFields,
      })
    );
    expect(response.status).toBe(503);
    const body = (await response.json()) as { error?: string };
    // TASK 014: the refusal names the platform fact and the workarounds
    // (export or direct upload) — never a pretend import.
    expect(body.error).toMatch(/no authorized VOD media-download endpoint/i);
    expect(body.error).toMatch(/export the VOD/i);
    expect(body.error).toMatch(/upload the file directly/i);
    // no job row may be created for a refused VOD
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("refuses Twitch live URLs with a distinct actionable 503", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "twitch",
        videoUrl: "https://twitch.tv/live/somechannel",
        ...clipFields,
      })
    );
    expect(response.status).toBe(503);
    const body = (await response.json()) as { error?: string };
    expect(body.error).toMatch(/live stream/i);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("creates a QUEUED job for a Twitch CLIP URL (worker imports via official Get Clips Download)", async () => {
    for (const clipUrl of [
      "https://clips.twitch.tv/RewolvedKleinerPinguin",
      "https://www.twitch.tv/clips/RewolvedKleinerPinguin",
      "https://twitch.tv/somechannel/clip/RewolvedKleinerPinguin",
    ]) {
      const response = await POST_JOBS(
        requestWithSession({ sourceType: "twitch", videoUrl: clipUrl, ...clipFields })
      );
      expect(response.status, clipUrl).toBe(201);
      const body = (await response.json()) as { job?: { id: string; status: string } };
      expect(body.job?.status).toBe("queued");
      expect(insertMock).toHaveBeenCalledWith(
        expect.objectContaining({
          sourceType: "twitch",
          sourceUrl: clipUrl,
          initialStatus: "queued",
        })
      );
      // reset per-iteration expectation counts
      vi.clearAllMocks();
      findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
      insertMock.mockResolvedValue({ id: JOB_ID });
      jobCreationRateLimiter.resetAll();
    }
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

describe("POST /api/v1/jobs — direct video file URLs (TASK 014)", () => {
  it("creates a QUEUED direct_url job for a public file-shaped video link", async () => {
    for (const videoUrl of [
      "https://storage.example.com/streams/vod.mp4",
      "https://cdn.mygame.example/archive/clip.webm?sig=abc123",
      "https://files.example.com/recording.mov",
    ]) {
      const response = await POST_JOBS(
        requestWithSession({ sourceType: "direct_url", videoUrl, ...clipFields })
      );
      expect(response.status, videoUrl).toBe(201);
      const body = (await response.json()) as { job?: { id: string; status: string } };
      expect(body.job?.status).toBe("queued");
      expect(insertMock).toHaveBeenCalledWith(
        expect.objectContaining({
          sourceType: "direct_url",
          sourceUrl: videoUrl,
          initialStatus: "queued",
        })
      );
      vi.clearAllMocks();
      findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
      insertMock.mockResolvedValue({ id: JOB_ID });
      jobCreationRateLimiter.resetAll();
    }
  });

  it("never echoes internal storage paths or signed URLs in the 201 response", async () => {
    const response = await POST_JOBS(
      requestWithSession({
        sourceType: "direct_url",
        videoUrl: "https://storage.example.com/v.mp4",
        ...clipFields,
      })
    );
    expect(response.status).toBe(201);
    const text = JSON.stringify(await response.json());
    expect(text).not.toContain("clipforge-sources");
    expect(text).not.toMatch(/sources\/[^"\\]+\/original/);
    expect(text).not.toMatch(/token=/i);
  });

  const rejectionCases: Array<[videoUrl: string | undefined, why: string]> = [
    [undefined, "missing videoUrl"],
    ["https://www.youtube.com/watch?v=abc", "platform URL (youtube) is not a direct file"],
    ["https://twitch.tv/videos/123", "platform URL (twitch) is not a direct file"],
    ["https://storage.example.com/page.html", "non-video extension"],
    ["https://storage.example.com/playlist.m3u8", "streaming playlist is not a file"],
    ["https://storage.example.com:8443/v.mp4", "non-standard port"],
    ["https://user:pass@storage.example.com/v.mp4", "credentials in URL"],
    ["not a url", "not a URL"],
  ];
  it.each(rejectionCases)("rejects direct_url inputs (%s) with 400", async (videoUrl) => {
    const response = await POST_JOBS(
      requestWithSession({ sourceType: "direct_url", videoUrl, ...clipFields })
    );
    expect(response.status).toBe(400);
    expect(insertMock).not.toHaveBeenCalled();
  });

  it("requires a verified session for direct_url jobs (401 without one)", async () => {
    const response = await POST_JOBS(
      requestWithoutSession({
        sourceType: "direct_url",
        videoUrl: "https://storage.example.com/v.mp4",
        ...clipFields,
      })
    );
    expect(response.status).toBe(401);
    expect(insertMock).not.toHaveBeenCalled();
  });
});

describe("classifyVideoUrl (TASK 014 shape classifier)", () => {
  it("classifies each URL family by shape only (no network)", () => {
    expect(classifyVideoUrl("https://www.youtube.com/watch?v=abc")).toBe("youtube");
    expect(classifyVideoUrl("https://youtu.be/abc123")).toBe("youtube");
    expect(classifyVideoUrl("https://twitch.tv/videos/123")).toBe("twitch-vod");
    expect(classifyVideoUrl("https://twitch.tv/chan/videos/123")).toBe("twitch-vod");
    expect(classifyVideoUrl("https://clips.twitch.tv/SomeClipSlug")).toBe("twitch-clip");
    expect(classifyVideoUrl("https://twitch.tv/chan/clip/SomeClipSlug")).toBe("twitch-clip");
    expect(classifyVideoUrl("https://twitch.tv/live/chan")).toBe("twitch-live");
    expect(classifyVideoUrl("https://twitch.tv/chan")).toBe("twitch-live");
    expect(classifyVideoUrl("https://storage.example.com/v.mp4")).toBe("direct-file");
    expect(classifyVideoUrl("https://storage.example.com/a/b/c.webm?s=1")).toBe("direct-file");
    expect(classifyVideoUrl("https://storage.example.com/v.mov")).toBe("direct-file");
  });

  it("never confuses lookalike domains with platforms (dot-boundary matching)", () => {
    expect(classifyVideoUrl("https://not-youtube.com/watch?v=x")).toBe("unrecognized");
    expect(classifyVideoUrl("https://youtube.com.evil.example/watch?v=x")).toBe("unrecognized");
    expect(classifyVideoUrl("https://twitch.tv.evil.example/videos/1")).toBe("unrecognized");
    expect(classifyVideoUrl("https://evil-twitch.tv/videos/1")).toBe("unrecognized");
    // ...and a video file on a lookalike host IS a direct file (guard applies at transfer).
    expect(classifyVideoUrl("https://storage.notyoutube.com/v.mp4")).toBe("direct-file");
  });

  it("rejects non-video files, non-http schemes, and non-URLs", () => {
    expect(classifyVideoUrl("https://example.com/page.html")).toBe("unrecognized");
    expect(classifyVideoUrl("https://example.com/playlist.m3u8")).toBe("unrecognized");
    expect(classifyVideoUrl("ftp://example.com/v.mp4")).toBe("unrecognized");
    expect(classifyVideoUrl("https://user:p@example.com/v.mp4")).toBe("unrecognized");
    expect(classifyVideoUrl("not a url")).toBe("unrecognized");
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
