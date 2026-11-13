/**
 * Results view state (Phase 6) — a PURE, DOM-free mapping from the results
 * API response to a UI phase.
 *
 * The React component renders whatever this module decides (and keeps
 * polling only while it says so), which is what makes the state machine
 * directly testable in the node test environment:
 *   loading → ready / processing / failed / not-found / auth-required / error
 */

export type ResultsJobStatus =
  | "uploading"
  | "queued"
  | "validating"
  | "ready"
  | "transcribing"
  | "analyzing"
  | "rendering"
  | "completed"
  | "failed";

export interface ResultsClipView {
  id: string;
  title: string;
  duration: number;
  sortOrder: number;
  status: "pending" | "processing" | "ready" | "failed";
  /** True only for ready clips with a verified storage object path. */
  downloadable: boolean;
}

export interface ResultsJobView {
  id: string;
  status: ResultsJobStatus;
  progress: number;
  createdAt: string;
  completedAt: string | null;
  /** Retention deadline (Phase 6): when the artifacts become eligible for cleanup. */
  expiresAt: string | null;
  clipsReady: number;
}

export interface ResultsZipView {
  /** False when there is nothing to zip or the set exceeds the size cap. */
  available: boolean;
  totalBytes: number;
  limitBytes: number;
}

export interface ResultsData {
  job: ResultsJobView;
  clips: ResultsClipView[];
  zip?: ResultsZipView;
}

export type ResultsState =
  | { phase: "loading" }
  | { phase: "auth-required" }
  | { phase: "not-found" }
  | { phase: "error"; message: string }
  | { phase: "processing"; data: ResultsData }
  | { phase: "failed"; data: ResultsData }
  | { phase: "ready"; data: ResultsData; expired: boolean };

export interface ResultsResponseInput {
  status: number;
  data: {
    job?: ResultsJobView;
    clips?: ResultsClipView[];
    zip?: ResultsZipView;
    error?: string;
  } | null;
}

/** True when `expiresAt` is present and strictly in the past. */
export function isExpired(expiresAt: string | null | undefined, now: Date): boolean {
  if (!expiresAt) return false;
  const t = Date.parse(expiresAt);
  return Number.isFinite(t) && t < now.getTime();
}

/** Maps one results-API response to the UI phase the page should render. */
export function interpretResults(input: ResultsResponseInput, now: Date): ResultsState {
  const { status, data } = input;

  if (status === 401) return { phase: "auth-required" };
  if (status === 404) return { phase: "not-found" };
  if (status < 200 || status >= 300 || !data?.job) {
    return { phase: "error", message: data?.error ?? "Could not load your results." };
  }

  const result: ResultsData = {
    job: data.job,
    clips: data.clips ?? [],
    zip: data.zip,
  };

  if (result.job.status === "failed") return { phase: "failed", data: result };
  if (result.job.status === "completed") {
    return { phase: "ready", data: result, expired: isExpired(result.job.expiresAt, now) };
  }
  // Any non-terminal status: still in the pipeline — keep polling.
  return { phase: "processing", data: result };
}

/** The page polls ONLY while the job is still moving through the pipeline. */
export function shouldPoll(state: ResultsState): boolean {
  return state.phase === "processing";
}

/**
 * When the ZIP button should be offered at all: a completed job with at
 * least one downloadable clip. The `zip.available` flag (size cap) and the
 * expired flag decide whether it is enabled or explained-disabled.
 */
export function downloadableClips(state: ResultsState): ResultsClipView[] {
  if (state.phase !== "ready") return [];
  return state.data.clips.filter((c) => c.downloadable);
}
