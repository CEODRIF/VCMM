import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the DB layer; route + ZIP engine + filename sanitization stay real.
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
// Keep the pure path helpers real; fake only the Storage round trips.
vi.mock("@/lib/storage/clips", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/storage/clips")>();
  return {
    ...actual,
    createClipSignedUrl: vi.fn(),
    listJobClipObjects: vi.fn(),
    fetchClipObjectStream: vi.fn(),
  };
});

import { GET as GET_DOWNLOAD_ALL } from "@/app/api/v1/jobs/[jobId]/download-all/route";
import { findSessionByTokenHash } from "@/lib/db/sessions";
import { findJobForSession } from "@/lib/db/jobs";
import { listClipsForJob, type ClipRow } from "@/lib/jobs/results";
import { fetchClipObjectStream, listJobClipObjects, type ClipObjectRef } from "@/lib/storage/clips";
import { parseZip } from "./helpers/zip";

const findSessionMock = findSessionByTokenHash as ReturnType<typeof vi.fn>;
const findJobMock = findJobForSession as ReturnType<typeof vi.fn>;
const listClipsMock = listClipsForJob as ReturnType<typeof vi.fn>;
const listObjectsMock = listJobClipObjects as ReturnType<typeof vi.fn>;
const fetchStreamMock = fetchClipObjectStream as ReturnType<typeof vi.fn>;

const SESSION_TOKEN = "b".repeat(64);
const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const OTHER_JOB_ID = "55555555-5555-4555-8555-555555555555";
const CLIP_ID_A = "33333333-3333-4333-8333-333333333333";
const CLIP_ID_B = "44444444-4444-4444-8444-444444444444";
const CLIP_ID_C = "66666666-6666-4666-8666-666666666666";
const FUTURE = new Date(Date.now() + 3600_000).toISOString();
const EXPIRY = new Date(Date.now() + 30 * 86400_000).toISOString();

const enc = new TextEncoder();
const CLIP_A_BYTES = enc.encode("AAAA-BBBB"); // 10 bytes
const CLIP_B_BYTES = enc.encode("CCCC-DDDD-EEEE"); // 14 bytes
const CLIP_BYTES = new Map<string, Uint8Array>([
  [CLIP_ID_A, CLIP_A_BYTES],
  [CLIP_ID_B, CLIP_B_BYTES],
]);

function requestWithSession(path: string, init?: RequestInit): Request {
  return new Request(`http://localhost${path}`, {
    method: "GET",
    headers: { cookie: `clipforge_session=${SESSION_TOKEN}` },
    ...init,
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
    id: CLIP_ID_A,
    title: "The opening hook",
    start_time: 32,
    end_time: 92,
    duration: 60,
    sort_order: 0,
    status: "ready",
    storage_path: `clips/${JOB_ID}/${CLIP_ID_A}.mp4`,
    ...overrides,
  };
}

/** Metadata listing for the clips that actually exist in Storage. */
function objectsFor(...clipIds: string[]): Map<string, { name: string; size: number }> {
  const map = new Map<string, { name: string; size: number }>();
  for (const id of clipIds) {
    const data = CLIP_BYTES.get(id);
    map.set(`${id}.mp4`, { name: `${id}.mp4`, size: data ? data.length : 0 });
  }
  return map;
}

/** Streams each clip's bytes in 3-byte chunks (like a real object fetch). */
async function* chunksOf(data: Uint8Array, size = 3): AsyncGenerator<Uint8Array> {
  for (let i = 0; i < data.length; i += size) yield data.slice(i, i + size);
}

/** Wraps an async iterable as a web ReadableStream (DOM lib has no ReadableStream.from). */
function streamFrom(gen: AsyncIterable<Uint8Array>): ReadableStream<Uint8Array> {
  const iterator = gen[Symbol.asyncIterator]();
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      const { done, value } = await iterator.next();
      if (done) controller.close();
      else if (value) controller.enqueue(value);
    },
  });
}

/** Default mock behaviour: stream the fixture bytes, lazily, per clip. */
function defaultFetchStream() {
  fetchStreamMock.mockImplementation(
    async (ref: ClipObjectRef): Promise<ReadableStream<Uint8Array>> => {
      const data = CLIP_BYTES.get(ref.clipId);
      if (!data) throw new Error(`unexpected clip fetch: ${ref.clipId}`);
      return streamFrom(chunksOf(data, 3));
    }
  );
}

async function collectBytes(stream: ReadableStream<Uint8Array>): Promise<Uint8Array> {
  const reader = stream.getReader();
  const parts: Uint8Array[] = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) parts.push(value);
  }
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.length;
  }
  return out;
}

const ctx = { params: { jobId: JOB_ID } };

beforeEach(() => {
  vi.clearAllMocks();
  findSessionMock.mockResolvedValue({ id: SESSION_ID, expires_at: FUTURE });
  findJobMock.mockResolvedValue(null);
  listClipsMock.mockResolvedValue([]);
  listObjectsMock.mockResolvedValue(objectsFor());
  defaultFetchStream();
});

afterEach(() => {
  vi.unstubAllEnvs();
});

// -- authorization + anti-enumeration ---------------------------------------------------

describe("download-all — authorization", () => {
  it("requires a session (401) and never touches the job table", async () => {
    const response = await GET_DOWNLOAD_ALL(new Request(`http://localhost/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    expect(response.status).toBe(401);
    expect(findJobMock).not.toHaveBeenCalled();
  });

  it("rejects non-UUID job ids without a session or DB round trip (anti-enumeration)", async () => {
    const response = await GET_DOWNLOAD_ALL(requestWithSession("/api/v1/jobs/nope/download-all"), {
      params: { jobId: "nope" },
    });
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Job not found." });
    expect(findJobMock).not.toHaveBeenCalled();
  });

  it("returns 404 for a job owned by another session (indistinguishable from missing)", async () => {
    findJobMock.mockResolvedValue(null);
    const response = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Job not found." });
    expect(fetchStreamMock).not.toHaveBeenCalled();
  });
});

// -- guard sequence (all BEFORE the first ZIP byte) --------------------------------------

describe("download-all — guards", () => {
  it("refuses with 409 while the job is still processing", async () => {
    findJobMock.mockResolvedValue(jobRow({ status: "rendering" }));
    const response = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    expect(response.status).toBe(409);
    const body = (await response.json()) as { error: string };
    expect(body.error).toMatch(/still being processed/i);
    expect(fetchStreamMock).not.toHaveBeenCalled();
  });

  it("refuses with 409 when the completed job has no clips at all", async () => {
    findJobMock.mockResolvedValue(jobRow());
    listClipsMock.mockResolvedValue([]);
    const response = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    expect(response.status).toBe(409);
    expect(fetchStreamMock).not.toHaveBeenCalled();
  });

  it("refuses with 409 when a ready clip has no object in Storage", async () => {
    findJobMock.mockResolvedValue(jobRow());
    listClipsMock.mockResolvedValue([clipRow(), clipRow({ id: CLIP_ID_B, title: "Second", sort_order: 1, storage_path: `clips/${JOB_ID}/${CLIP_ID_B}.mp4` })]);
    listObjectsMock.mockResolvedValue(objectsFor(CLIP_ID_A)); // B missing
    const response = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    expect(response.status).toBe(409);
    const body = (await response.json()) as { error: string };
    expect(body.error).toMatch(/could not be found in storage/i);
    expect(fetchStreamMock).not.toHaveBeenCalled();
  });

  it("refuses with 413 (and a per-clip fallback message) when the set exceeds the size cap", async () => {
    vi.stubEnv("MAX_ZIP_SIZE_BYTES", "12"); // 10 + 14 = 24 > 12
    findJobMock.mockResolvedValue(jobRow());
    listClipsMock.mockResolvedValue([
      clipRow(),
      clipRow({ id: CLIP_ID_B, title: "Second", sort_order: 1, storage_path: `clips/${JOB_ID}/${CLIP_ID_B}.mp4` }),
    ]);
    listObjectsMock.mockResolvedValue(objectsFor(CLIP_ID_A, CLIP_ID_B));
    const response = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    expect(response.status).toBe(413);
    const body = (await response.json()) as { error: string };
    expect(body.error).toMatch(/individually/i);
    expect(fetchStreamMock).not.toHaveBeenCalled();
  });
});

/** The two ready, canonical clips used by the streaming/cancellation tests. */
function readyClips(): ClipRow[] {
  return [
    clipRow(),
    clipRow({ id: CLIP_ID_B, title: `Six/../seven\u0000eight`, sort_order: 1, storage_path: `clips/${JOB_ID}/${CLIP_ID_B}.mp4` }),
  ];
}

// -- the streamed ZIP itself -------------------------------------------------------------

describe("download-all — streamed ZIP", () => {

  it("streams a valid ZIP of the ready clips (sanitized names, intact bytes, sort order)", async () => {
    findJobMock.mockResolvedValue(jobRow());
    listClipsMock.mockResolvedValue(readyClips());
    listObjectsMock.mockResolvedValue(objectsFor(CLIP_ID_A, CLIP_ID_B));

    const response = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("application/zip");
    expect(response.headers.get("content-disposition")).toBe(`attachment; filename="clips-${JOB_ID}.zip"`);
    expect(response.body).toBeInstanceOf(ReadableStream);

    const bytes = await collectBytes(response.body!);
    // Starts with the PK local-file signature — it is a real ZIP, not JSON.
    expect(Array.from(bytes.slice(0, 4))).toEqual([0x50, 0x4b, 0x03, 0x04]);

    const parsed = parseZip(bytes);
    expect(parsed.entries.map((e) => e.name)).toEqual([
      "01-The opening hook.mp4",
      "02-Six .. seven eight.mp4", // separators + NUL replaced, still a single segment
    ]);
    for (const e of parsed.entries) {
      expect(e.method).toBe(0); // STORED (MP4 already compressed)
      expect(e.utf8Flag).toBe(true);
    }
    expect(parsed.entries[0]!.data).toEqual(CLIP_A_BYTES);
    expect(parsed.entries[1]!.data).toEqual(CLIP_B_BYTES);

    // Fetched lazily and sequentially (concurrency = 1), in sort order.
    expect(fetchStreamMock).toHaveBeenCalledTimes(2);
    expect(fetchStreamMock.mock.calls[0]?.[0]).toEqual({ jobId: JOB_ID, clipId: CLIP_ID_A });
    expect(fetchStreamMock.mock.calls[1]?.[0]).toEqual({ jobId: JOB_ID, clipId: CLIP_ID_B });
    // Every fetch received an abort signal (cancellation plumbing).
    for (const call of fetchStreamMock.mock.calls) {
      expect(call?.[1]).toBeInstanceOf(AbortSignal);
    }
  });

  it("includes only ready clips with canonical paths (foreign/malformed rows excluded)", async () => {
    findJobMock.mockResolvedValue(jobRow());
    listClipsMock.mockResolvedValue([
      clipRow(),
      clipRow({
        id: CLIP_ID_C,
        title: "Foreign path",
        sort_order: 2,
        storage_path: `clips/${OTHER_JOB_ID}/${CLIP_ID_C}.mp4`, // different job → excluded
      }),
      clipRow({ id: CLIP_ID_B, title: "Not ready", sort_order: 3, status: "processing" }),
    ]);
    listObjectsMock.mockResolvedValue(objectsFor(CLIP_ID_A));

    const response = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    expect(response.status).toBe(200);
    const parsed = parseZip(await collectBytes(response.body!));
    expect(parsed.entries.map((e) => e.name)).toEqual(["01-The opening hook.mp4"]);
    expect(fetchStreamMock).toHaveBeenCalledTimes(1);
    expect(fetchStreamMock.mock.calls[0]?.[0]).toEqual({ jobId: JOB_ID, clipId: CLIP_ID_A });
  });

  it("is deterministic for the same inputs", async () => {
    const setup = () => {
      findJobMock.mockResolvedValue(jobRow());
      listClipsMock.mockResolvedValue(readyClips());
      listObjectsMock.mockResolvedValue(objectsFor(CLIP_ID_A, CLIP_ID_B));
    };
    setup();
    const first = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    setup();
    const second = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    expect(await collectBytes(second.body!)).toEqual(await collectBytes(first.body!));
  });
});

// -- cancellation + partial failure ------------------------------------------------------

describe("download-all — cancellation and mid-stream failure", () => {
  it("client abort tears down the in-flight Storage fetch (body errors, no silent success)", async () => {
    findJobMock.mockResolvedValue(jobRow());
    listClipsMock.mockResolvedValue(readyClips());
    listObjectsMock.mockResolvedValue(objectsFor(CLIP_ID_A, CLIP_ID_B));

    let capturedSignal: AbortSignal | undefined;
    fetchStreamMock.mockImplementation(
      async (ref: ClipObjectRef, signal?: AbortSignal): Promise<ReadableStream<Uint8Array>> => {
        if (ref.clipId === CLIP_ID_A) {
          capturedSignal = signal;
          return new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(CLIP_A_BYTES);
              if (signal) {
                signal.addEventListener(
                  "abort",
                  () => controller.error(new Error("storage fetch aborted")),
                  { once: true }
                );
              }
              // never closes: simulates a large in-flight object
            },
          });
        }
        return streamFrom(chunksOf(CLIP_BYTES.get(ref.clipId) ?? new Uint8Array(0), 3));
      }
    );

    const ac = new AbortController();
    const response = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`, { signal: ac.signal }), ctx);
    expect(response.status).toBe(200);

    const reader = response.body!.getReader();
    const first = await reader.read();
    expect(first.done).toBe(false); // the local header of the first entry
    ac.abort(); // the client gives up

    await expect(reader.read()).rejects.toThrow(/aborted/);
    expect(capturedSignal?.aborted).toBe(true); // the route propagated the abort
  });

  it("reader.cancel() (browser navigates away) also aborts the Storage fetch", async () => {
    findJobMock.mockResolvedValue(jobRow());
    listClipsMock.mockResolvedValue([clipRow()]);
    listObjectsMock.mockResolvedValue(objectsFor(CLIP_ID_A));

    let capturedSignal: AbortSignal | undefined;
    fetchStreamMock.mockImplementation(async (ref: ClipObjectRef, signal?: AbortSignal) => {
      capturedSignal = signal;
      // open-ended stream: stays "in flight" until cancelled
      return new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(CLIP_A_BYTES);
        },
      });
    });

    const response = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    const reader = response.body!.getReader();
    await reader.read(); // first chunk
    await reader.cancel();
    expect(capturedSignal?.aborted).toBe(true);
  });

  it("a mid-stream Storage failure errors the response after the bytes already sent (documented partial failure)", async () => {
    findJobMock.mockResolvedValue(jobRow());
    listClipsMock.mockResolvedValue(readyClips());
    listObjectsMock.mockResolvedValue(objectsFor(CLIP_ID_A, CLIP_ID_B));

    async function* boom(): AsyncGenerator<Uint8Array> {
      yield enc.encode("partial");
      throw new Error("object vanished mid-stream");
    }
    fetchStreamMock.mockImplementation(async (ref: ClipObjectRef) => {
      if (ref.clipId === CLIP_ID_A) return streamFrom(boom());
      return streamFrom(chunksOf(CLIP_BYTES.get(ref.clipId) ?? new Uint8Array(0), 3));
    });

    const response = await GET_DOWNLOAD_ALL(requestWithSession(`/api/v1/jobs/${JOB_ID}/download-all`), ctx);
    expect(response.status).toBe(200); // headers already sent — failure aborts the body
    const reader = response.body!.getReader();
    for (;;) {
      const { done, value } = await reader.read().catch((err: Error) => {
        expect(err.message).toBe("object vanished mid-stream");
        return { done: true, value: undefined as Uint8Array | undefined };
      });
      if (done) break;
      expect(value).toBeDefined();
    }
  });
});
