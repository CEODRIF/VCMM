/**
 * Results UI state machine (Phase 6) — the pure mapping from a results-API
 * response to the view phase. This is what the page renders, so the states
 * (loading / auth / not-found / error / processing / failed / ready /
 * expired) are exercised here in the node test environment.
 */
import { describe, expect, it } from "vitest";
import {
  downloadableClips,
  interpretResults,
  isExpired,
  shouldPoll,
  type ResultsData,
  type ResultsJobView,
  type ResultsResponseInput,
} from "@/lib/results/view";

const NOW = new Date("2026-11-13T12:00:00.000Z");
const JOB: ResultsJobView = {
  id: "22222222-2222-4222-8222-222222222222",
  status: "completed",
  progress: 100,
  createdAt: "2026-11-13T00:00:00.000Z",
  completedAt: "2026-11-13T01:00:00.000Z",
  expiresAt: "2026-12-13T01:00:00.000Z", // 30 days after completion
  clipsReady: 2,
};

function data(overrides: Partial<ResultsData> = {}): ResultsData {
  return {
    job: JOB,
    clips: [
      { id: "c1", title: "One", duration: 60, sortOrder: 0, status: "ready", downloadable: true },
      { id: "c2", title: "Two", duration: 90, sortOrder: 1, status: "ready", downloadable: true },
    ],
    zip: { available: true, totalBytes: 1000, limitBytes: 2000 },
    ...overrides,
  };
}

function response(status: number, body: object | null): ResultsResponseInput {
  return { status, data: body as ResultsResponseInput["data"] };
}

describe("interpretResults — transport outcomes", () => {
  it("maps 401 to auth-required (before anything else is inspected)", () => {
    expect(interpretResults(response(401, { error: "nope" }), NOW)).toEqual({ phase: "auth-required" });
    expect(shouldPoll(interpretResults(response(401, null), NOW))).toBe(false);
  });

  it("maps 404 to not-found (foreign or missing job — same UI)", () => {
    expect(interpretResults(response(404, { error: "Job not found." }), NOW)).toEqual({ phase: "not-found" });
  });

  it("maps other non-2xx to error with the server message", () => {
    const state = interpretResults(response(500, { error: "Internal server error." }), NOW);
    expect(state).toEqual({ phase: "error", message: "Internal server error." });
  });

  it("maps non-2xx without a body to a generic error message", () => {
    const state = interpretResults(response(502, null), NOW);
    expect(state).toEqual({ phase: "error", message: "Could not load your results." });
  });

  it("maps 200 without a job payload to error (never a half-render)", () => {
    const state = interpretResults(response(200, {}), NOW);
    expect(state.phase).toBe("error");
  });
});

describe("interpretResults — job outcomes", () => {
  it("maps a completed job to ready (not expired while the deadline is in the future)", () => {
    const state = interpretResults(response(200, data()), NOW);
    expect(state.phase).toBe("ready");
    if (state.phase === "ready") {
      expect(state.expired).toBe(false);
      expect(state.data.job.id).toBe(JOB.id);
      expect(state.data.clips).toHaveLength(2);
    }
    expect(shouldPoll(state)).toBe(false);
  });

  it("maps a completed job with a past deadline to ready + expired", () => {
    const state = interpretResults(
      response(200, data({ job: { ...JOB, expiresAt: "2026-11-01T00:00:00.000Z" } })),
      NOW
    );
    expect(state).toMatchObject({ phase: "ready", expired: true });
  });

  it("treats a missing deadline as not expired (no guessed retention)", () => {
    const state = interpretResults(response(200, data({ job: { ...JOB, expiresAt: null } })), NOW);
    expect(state).toMatchObject({ phase: "ready", expired: false });
  });

  it("maps every non-terminal status to processing (polling continues)", () => {
    const statuses = [
      "uploading", "queued", "validating", "ready", "transcribing", "analyzing", "rendering",
    ] as const;
    for (const status of statuses) {
      const state = interpretResults(
        response(200, data({ job: { ...JOB, status, progress: 40 } })),
        NOW
      );
      expect(state, status).toEqual({ phase: "processing", data: expect.objectContaining({ job: expect.objectContaining({ status }) }) });
      expect(shouldPoll(state), status).toBe(true);
    }
  });

  it("maps a failed job to failed (polling stops)", () => {
    const state = interpretResults(response(200, data({ job: { ...JOB, status: "failed" } })), NOW);
    expect(state.phase).toBe("failed");
    expect(shouldPoll(state)).toBe(false);
  });

  it("treats an empty clips array as data (the ready state renders the empty view)", () => {
    const state = interpretResults(response(200, { job: JOB, clips: [] }), NOW);
    expect(state).toMatchObject({ phase: "ready", expired: false });
    if (state.phase === "ready") {
      expect(state.data.clips).toEqual([]);
      expect(downloadableClips(state)).toEqual([]);
    }
  });
});

describe("isExpired — boundary", () => {
  it("deadline exactly at 'now' is NOT expired (strictly past only)", () => {
    expect(isExpired("2026-11-13T12:00:00.000Z", NOW)).toBe(false);
    expect(isExpired("2026-11-13T11:59:59.999Z", NOW)).toBe(true);
  });

  it("unparsable deadlines are treated as not expired (fail visible, not hidden)", () => {
    expect(isExpired("not-a-date", NOW)).toBe(false);
    expect(isExpired(null, NOW)).toBe(false);
    expect(isExpired(undefined, NOW)).toBe(false);
  });
});

describe("downloadableClips — only ready + verified clips", () => {
  it("filters non-downloadable clips out of the grid", () => {
    const state = interpretResults(
      response(
        200,
        data({
          clips: [
            { id: "c1", title: "One", duration: 60, sortOrder: 0, status: "ready", downloadable: true },
            { id: "c2", title: "Two", duration: 90, sortOrder: 1, status: "ready", downloadable: false },
            { id: "c3", title: "Three", duration: 75, sortOrder: 2, status: "failed", downloadable: false },
          ],
        })
      ),
      NOW
    );
    if (state.phase !== "ready") throw new Error("expected ready");
    expect(downloadableClips(state).map((c) => c.id)).toEqual(["c1"]);
  });

  it("returns nothing outside the ready phase", () => {
    expect(downloadableClips({ phase: "processing", data: data() })).toEqual([]);
    expect(downloadableClips({ phase: "loading" })).toEqual([]);
    expect(downloadableClips({ phase: "failed", data: data() })).toEqual([]);
  });
});
