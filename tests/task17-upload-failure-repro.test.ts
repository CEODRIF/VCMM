/**
 * TASK 017 — production upload-failure investigation (evidence).
 *
 * Reproduction of the reported incident at the server boundary, running the
 * REAL code path end-to-end:
 *   route handler → toErrorResponse → jobs service → real db/jobs wrappers
 *   → real storage/objects wrappers → (only the Supabase transport is faked)
 *
 * The incident: a 250 MB MP4 upload failed at the browser→storage transfer
 * (the form showed "Job … is waiting for its file" + Retry upload); a later
 * "Start Creating" click surfaced the generic "Internal server error."
 *
 * Proven here:
 *   - EVERY known failure class of the three upload endpoints (job
 *     creation, authorize, complete) returns an ACTIONABLE typed response
 *     (503 database / 502 storage) that states exactly what did NOT
 *     happen — a failed operation can never be mistaken for completed;
 *   - the generic 500 "Internal server error." remains ONLY as the
 *     safety net for genuinely unexpected errors (covered separately in
 *     jobs-api-hardening.test.ts);
 *   - the browser-side transfer failure mapping (413/403/404/5xx/network)
 *     is actionable and sanitizes storage response bodies (no URLs or
 *     tokens ever reach the UI).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => {
  type Result = { data: unknown; error: unknown };
  const behavior: Record<string, (values?: unknown) => Result> = {
    "jobs:insert": () => ({ data: { id: "11111111-1111-4111-8111-111111111111" }, error: null }),
    "jobs:select": () => ({ data: null, error: null }),
    "jobs:update": () => ({ data: [{ id: "22222222-2222-4222-8222-222222222222" }], error: null }),
    "clips:select": () => ({ data: [], error: null }),
    "storage:signed": () => ({
      data: { signedUrl: "https://storage.example.invalid/signed/upload" },
      error: null,
    }),
    "storage:list": () => ({
      data: [{ name: "obj.mp4", metadata: { size: 262_144_000, mimetype: "video/mp4" }, updated_at: null }],
      error: null,
    }),
  };
  const behaviorFor = (key: string): ((values?: unknown) => Result) => behavior[key]!;
  return { behavior, behaviorFor };
});

vi.mock("@/lib/supabase/server", () => {
  function tableBuilder(name: string) {
    let lastOp: "insert" | "update" | null = null;
    let lastValues: unknown = null;
    const key = (op: string) => `${name}:${op}`;
    const chain = {
      eq: () => chain,
      in: () => chain,
      order: () => chain,
      limit: () => chain,
      insert(v: unknown) {
        lastOp = "insert";
        lastValues = v;
        return chain;
      },
      update(v: unknown) {
        lastOp = "update";
        lastValues = v;
        return chain;
      },
      select(_cols: string) {
        if (lastOp === "insert") {
          const promise = Promise.resolve(state.behaviorFor(key("insert"))(lastValues));
          return {
            single: async () => promise,
            then: (res: (v: unknown) => unknown, rej: (e: unknown) => unknown) => promise.then(res, rej),
          };
        }
        if (lastOp === "update") {
          const promise = Promise.resolve(state.behaviorFor(key("update"))(lastValues));
          return {
            then: (res: (v: unknown) => unknown, rej: (e: unknown) => unknown) => promise.then(res, rej),
          };
        }
        return chain;
      },
      single: async () => state.behaviorFor(key("insert"))(lastValues),
      maybeSingle: async () => state.behaviorFor(key("select"))(lastValues),
      then: (res: (v: unknown) => unknown, rej: (e: unknown) => unknown) =>
        Promise.resolve(state.behaviorFor(key("select"))(lastValues)).then(res, rej),
    };
    return chain;
  }

  return {
    getSupabaseServerClient: () => ({
      from: (name: string) => tableBuilder(name),
      storage: {
        from: (_bucket: string) => ({
          createSignedUploadUrl: async () => state.behaviorFor("storage:signed")(),
          list: async () => state.behaviorFor("storage:list")(),
        }),
      },
    }),
  };
});

vi.mock("@/lib/db/sessions", () => ({
  findSessionByTokenHash: vi.fn(),
  createSessionRow: vi.fn(),
  deleteSessionRow: vi.fn(),
}));

import { POST as CREATE_JOB } from "@/app/api/v1/jobs/route";
import { POST as AUTHORIZE } from "@/app/api/v1/jobs/[jobId]/upload/authorize/route";
import { POST as COMPLETE } from "@/app/api/v1/jobs/[jobId]/upload/complete/route";
import { describeStorageTransferFailure } from "@/lib/api/storage-errors";
import { findSessionByTokenHash } from "@/lib/db/sessions";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;

const SESSION_TOKEN = "a".repeat(64);
const SESSION_ID = "33333333-3333-4333-8333-333333333333";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const FUTURE = new Date(Date.now() + 3600_000).toISOString();

function jobRow(overrides: Record<string, unknown> = {}) {
  return {
    id: JOB_ID,
    session_id: SESSION_ID,
    source_type: "upload",
    source_url: null,
    source_path: `sources/${JOB_ID}/original/obj.mp4`,
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
    source_metadata: { original_file_name: "lecture.mp4", declared_size_bytes: 262_144_000 },
    updated_at: FUTURE,
    ...overrides,
  };
}

function request(path: string, body: unknown): Request {
  return new Request(`http://localhost${path}`, {
    method: "POST",
    headers: {
      cookie: `clipforge_session=${SESSION_TOKEN}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  });
}

function resetBehavior() {
  state.behavior["jobs:insert"] = () => ({ data: { id: JOB_ID }, error: null });
  state.behavior["jobs:select"] = () => ({ data: null, error: null });
  state.behavior["jobs:update"] = () => ({ data: [{ id: JOB_ID }], error: null });
  state.behavior["clips:select"] = () => ({ data: [], error: null });
  state.behavior["storage:signed"] = () => ({
    data: { signedUrl: "https://storage.example.invalid/signed/upload" },
    error: null,
  });
  state.behavior["storage:list"] = () => ({
    data: [{ name: "obj.mp4", metadata: { size: 262_144_000, mimetype: "video/mp4" } }],
    error: null,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  resetBehavior();
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
});

const CREATE_PAYLOAD = {
  sourceType: "upload",
  fileName: "lecture.mp4",
  clipCount: 3,
  clipDurationSeconds: 120,
  captionsEnabled: true,
  generationMode: "highlights",
  outputFps: 30,
};
const AUTH_PATH = `/api/v1/jobs/${JOB_ID}/upload/authorize`;
const COMPLETE_PATH = `/api/v1/jobs/${JOB_ID}/upload/complete`;
const SIZE_250MB = 262_144_000; // the incident file size

describe("reproduction: the three upload endpoints under failure", () => {
  it("job creation: DB down → actionable 503 (NOT 'Internal server error.')", async () => {
    state.behavior["jobs:insert"] = () => ({
      data: null,
      error: { code: "08006", message: "connection failure" },
    });
    const response = await CREATE_JOB(request("/api/v1/jobs", CREATE_PAYLOAD));
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(503);
    expect(body.error).toMatch(/database is temporarily unavailable/i);
    expect(body.error).toMatch(/was NOT created/i);
    expect(body.error).not.toBe("Internal server error.");
  });

  it("authorize: storage down → actionable 502, file explicitly NOT uploaded", async () => {
    state.behavior["jobs:select"] = () => ({ data: jobRow(), error: null });
    state.behavior["storage:signed"] = () => ({
      data: null,
      error: { message: "Bucket not found" },
    });
    const response = await AUTHORIZE(
      request(AUTH_PATH, { fileName: "lecture.mp4", sizeBytes: SIZE_250MB, mimeType: "video/mp4" }),
      { params: { jobId: JOB_ID } }
    );
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(502);
    expect(body.error).toMatch(/private storage is temporarily unavailable/i);
    expect(body.error).toMatch(/was NOT uploaded/i);
    expect(body.error).not.toBe("Internal server error.");
    // The diagnostic ("Bucket not found") never reaches the client.
    expect(body.error).not.toContain("Bucket not found");
  });

  it("authorize: DB down (authorization not saved) → actionable 503", async () => {
    state.behavior["jobs:select"] = () => ({ data: jobRow(), error: null });
    state.behavior["jobs:update"] = () => ({
      data: null,
      error: { code: "08006", message: "connection failure" },
    });
    const response = await AUTHORIZE(
      request(AUTH_PATH, { fileName: "lecture.mp4", sizeBytes: SIZE_250MB, mimeType: "video/mp4" }),
      { params: { jobId: JOB_ID } }
    );
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(503);
    expect(body.error).toMatch(/authorization was NOT saved/i);
  });

  it("complete: storage down → actionable 502, job explicitly NOT completed", async () => {
    state.behavior["jobs:select"] = () => ({ data: jobRow(), error: null });
    state.behavior["storage:list"] = () => ({
      data: null,
      error: { message: "storage unavailable" },
    });
    const response = await COMPLETE(request(COMPLETE_PATH, { sizeBytes: SIZE_250MB }), {
      params: { jobId: JOB_ID },
    });
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(502);
    expect(body.error).toMatch(/could not be verified/i);
    expect(body.error).toMatch(/was NOT completed/i);
    expect(body.error).not.toBe("Internal server error.");
  });

  it("complete: object missing → typed 409 (regression: never a 500)", async () => {
    state.behavior["jobs:select"] = () => ({ data: jobRow(), error: null });
    state.behavior["storage:list"] = () => ({ data: [], error: null });
    const response = await COMPLETE(request(COMPLETE_PATH, { sizeBytes: SIZE_250MB }), {
      params: { jobId: JOB_ID },
    });
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(409);
    expect(body.error).toMatch(/Uploaded object not found/i);
  });

  it("complete: DB down → actionable 503, upload explicitly NOT marked complete", async () => {
    state.behavior["jobs:select"] = () => ({ data: jobRow(), error: null });
    state.behavior["jobs:update"] = () => ({
      data: null,
      error: { code: "08006", message: "connection failure" },
    });
    const response = await COMPLETE(request(COMPLETE_PATH, { sizeBytes: SIZE_250MB }), {
      params: { jobId: JOB_ID },
    });
    const body = (await response.json()) as { error: string };
    expect(response.status).toBe(503);
    expect(body.error).toMatch(/was NOT marked complete/i);
  });

  it("happy path (250 MB within limits): create → authorize → complete", async () => {
    const created = await CREATE_JOB(request("/api/v1/jobs", CREATE_PAYLOAD));
    expect(created.status).toBe(201);

    state.behavior["jobs:select"] = () => ({ data: jobRow(), error: null });
    const auth = await AUTHORIZE(
      request(AUTH_PATH, { fileName: "lecture.mp4", sizeBytes: SIZE_250MB, mimeType: "video/mp4" }),
      { params: { jobId: JOB_ID } }
    );
    expect(auth.status).toBe(200);
    const authBody = (await auth.json()) as { uploadUrl: string };
    expect(authBody.uploadUrl).toBe("https://storage.example.invalid/signed/upload");

    // After finalization the re-read returns the queued job.
    state.behavior["jobs:select"] = () => ({ data: jobRow({ status: "queued", upload_completed_at: FUTURE }), error: null });
    const done = await COMPLETE(request(COMPLETE_PATH, { sizeBytes: SIZE_250MB }), {
      params: { jobId: JOB_ID },
    });
    expect(done.status).toBe(200);
    const doneBody = (await done.json()) as { enqueued: boolean };
    expect(doneBody.enqueued).toBe(true);
  });
});

describe("browser transfer failure mapping (the 250 MB PUT itself)", () => {
  it("413 → the file exceeded the storage limit (actionable)", () => {
    const msg = describeStorageTransferFailure(413, '{"statusCode":413,"message":"File size exceeds the allowed limit"}');
    expect(msg).toMatch(/too large/i);
    expect(msg).toMatch(/smaller file|raise the limit/i);
    expect(msg).toContain("File size exceeds the allowed limit");
  });

  it("403 → the link was rejected; re-authorize", () => {
    const msg = describeStorageTransferFailure(403, null);
    expect(msg).toMatch(/fresh authorization/i);
  });

  it("404 → destination not found; retry", () => {
    expect(describeStorageTransferFailure(404)).toMatch(/destination could not be found/i);
  });

  it("5xx → storage temporarily unavailable; file NOT uploaded", () => {
    for (const status of [500, 502, 503]) {
      const msg = describeStorageTransferFailure(status);
      expect(msg).toMatch(/temporarily unavailable/i);
      expect(msg).toMatch(/was NOT uploaded/i);
    }
  });

  it("network drop (no status) → transfer incomplete; retry", () => {
    const msg = describeStorageTransferFailure(null);
    expect(msg).toMatch(/network connection dropped/i);
    expect(msg).toMatch(/was NOT fully uploaded/i);
  });

  it("unknown status → generic but explicit (NOT uploaded)", () => {
    const msg = describeStorageTransferFailure(418);
    expect(msg).toMatch(/HTTP 418/);
    expect(msg).toMatch(/was NOT uploaded/i);
  });

  it("never echoes URLs or tokens from storage response bodies", () => {
    const body = '{"message":"see https://signed.example.invalid/upload?token=SECRET123 for details"}';
    const msg = describeStorageTransferFailure(403, body);
    expect(msg).not.toContain("https://");
    expect(msg).not.toContain("SECRET123");
    const plain = "Refused by https://proxy.example.invalid/edge?auth=TOKEN999";
    const msg2 = describeStorageTransferFailure(500, plain);
    expect(msg2).not.toContain("https://");
    expect(msg2).not.toContain("TOKEN999");
  });
});
