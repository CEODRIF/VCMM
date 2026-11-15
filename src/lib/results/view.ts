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
  status: "pending" | "processing" | "ready" | "failed" | "deleting";
  /** True only for ready clips with a verified storage object path. */
  downloadable: boolean;
  /**
   * TASK 015 P4 — exact source timestamps + the MEASURED output contract.
   * All optional: absent/`null` means "not available" (the UI shows "—",
   * never a guessed value).
   */
  startTime?: number | null;
  endTime?: number | null;
  mode?: "highlights" | "full_coverage" | null;
  width?: number | null;
  height?: number | null;
  outputFps?: number | null;
  fileSizeBytes?: number | null;
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
  /** TASK 015 P4 — cleanup state (Phase 6 marker reused). */
  purgedAt?: string | null;
  /** Non-NULL = a user cleanup failed partway (visible + retryable). */
  cleanupError?: string | null;
  /** TASK 015 P3 — generation settings (display only). */
  generationMode?: "highlights" | "full_coverage" | null;
  outputFps?: number | null;
  sourceDurationSeconds?: number | null;
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

// ---------------------------------------------------------------------------
// TASK 015 P4 — job-level cleanup state helpers
// ---------------------------------------------------------------------------

/** True once the job's artifacts were purged (manual purge or user cleanup). */
export function isPurged(state: ResultsState): boolean {
  if (state.phase !== "ready" && state.phase !== "failed") return false;
  return state.data.job.purgedAt != null;
}

/** A user cleanup failed partway — visible + retryable (the job stays
 *  un-purged while this is true). */
export function cleanupIncomplete(state: ResultsState): boolean {
  if (state.phase !== "ready" && state.phase !== "failed") return false;
  return state.data.job.purgedAt == null && state.data.job.cleanupError != null;
}

/** The "Delete all / cleanup" action is available for a TERMINAL job that
 *  has not been purged yet and whose artifacts may still exist. */
export function canCleanup(state: ResultsState): boolean {
  if (state.phase !== "ready" && state.phase !== "failed") return false;
  return state.data.job.purgedAt == null;
}
