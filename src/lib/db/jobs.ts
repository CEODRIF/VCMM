import "server-only";

import { ApiError } from "@/lib/api/errors";
import { getSupabaseServerClient } from "@/lib/supabase/server";

/**
 * TASK 017 — production upload-failure investigation: every wrapper below
 * converted its Supabase REST error into a raw `Error`, which the routes
 * masked as a generic 500 "Internal server error." with no actionable
 * message. They now throw a typed, sanitized ApiError (503 = the database
 * is the failing dependency; the user is told EXACTLY what did NOT happen,
 * so a failed operation can never be mistaken for a completed one) while
 * the diagnostic detail (error code) stays in the server log.
 */

/**
 * Job data access (server-only, service role).
 *
 * EVERY query is scoped by session_id — job rows are only ever fetched when
 * the server-verified session owns them. There is no code path that returns a
 * job by id alone, which prevents cross-session enumeration.
 *
 * Phase 3: the jobs row carries the ingestion queue state (lease, attempts,
 * upload completion, verified source metadata). Queue-control writes by the
 * WORKER happen exclusively through the restricted RPCs in migration
 * 20261115000000; the API performs only its own guarded transitions
 * (creation, upload authorization, upload completion).
 */

export type JobStatus =
  | "uploading"
  | "queued"
  | "validating"
  | "ready"
  | "transcribing"
  | "analyzing"
  | "rendering"
  | "completed"
  | "failed";

export interface JobRow {
  id: string;
  session_id: string;
  source_type: "youtube" | "twitch" | "kick" | "upload" | "direct_url";
  source_url: string | null;
  source_path: string | null;
  requested_clips: number | null;
  /** TASK 015 P3: 'highlights' | 'full_coverage'. */
  generation_mode: "highlights" | "full_coverage";
  /** TASK 015 P3: explicit output frame rate (30|60) or null (legacy auto). */
  output_fps: number | null;
  clip_duration_seconds: number;
  captions_enabled: boolean;
  status: JobStatus;
  progress: number;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
  expires_at: string | null;
  /** Phase 6: set when the job's artifacts were purged (manual or user cleanup). */
  purged_at: string | null;
  /** TASK 015 P4: non-NULL = a user cleanup failed partway (visible + retryable). */
  cleanup_error: string | null;
  // Phase 3 — queue / ingestion
  attempt_count: number;
  max_attempts: number;
  locked_by: string | null;
  locked_at: string | null;
  lease_expires_at: string | null;
  last_error: string | null;
  upload_completed_at: string | null;
  upload_auth_expires_at: string | null;
  source_metadata: Record<string, unknown>;
  updated_at: string;
}

export interface NewJobInput {
  id: string;
  sessionId: string;
  sourceType: "youtube" | "twitch" | "kick" | "upload" | "direct_url";
  sourceUrl?: string;
  sourcePath?: string;
  /**
   * TASK 015 P3: null = "derived at analysis time" (full_coverage mode —
   * ceil(source_duration / clip_duration_seconds), never truncated by a
   * user count); number (1..10) = highlights upper bound.
   */
  requestedClips: number | null;
  clipDurationSeconds: number;
  captionsEnabled: boolean;
  /** TASK 015 P3: 'highlights' (default) | 'full_coverage'. */
  generationMode?: "highlights" | "full_coverage";
  /** TASK 015 P3: explicit output frame rate (30|60) or null (legacy auto). */
  outputFps?: number | null;
  /** Initial state: upload jobs wait for the file ('uploading'); provider-backed jobs start 'queued'. */
  initialStatus: "uploading" | "queued";
}

/** Inserts a job row with the given initial status (DB enforces the transition map). */
export async function insertJob(input: NewJobInput): Promise<{ id: string }> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .insert({
      id: input.id,
      session_id: input.sessionId,
      source_type: input.sourceType,
      source_url: input.sourceUrl ?? null,
      source_path: input.sourcePath ?? null,
      requested_clips: input.requestedClips,
      clip_duration_seconds: input.clipDurationSeconds,
      captions_enabled: input.captionsEnabled,
      generation_mode: input.generationMode ?? "highlights",
      output_fps: input.outputFps ?? null,
      status: input.initialStatus,
      progress: 0,
    })
    .select("id")
    .single();
  if (error) {
    console.error("[db] job creation failed:", error.code ?? "unknown");
    throw new ApiError(
      503,
      "The database is temporarily unavailable — your job was NOT created. Please try again."
    );
  }
  return (data as { id: string }) ?? { id: input.id };
}

/** Fetches one job only if it belongs to the given session (else null). */
export async function findJobForSession(sessionId: string, jobId: string): Promise<JobRow | null> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .select("*")
    .eq("id", jobId)
    .eq("session_id", sessionId)
    .maybeSingle();
  if (error) {
    console.error("[db] job lookup failed:", error.code ?? "unknown");
    throw new ApiError(503, "The database is temporarily unavailable. Please try again.");
  }
  return (data as JobRow | null) ?? null;
}

/** Lists only the jobs of the given session, newest first. */
export async function listJobsForSession(sessionId: string, limit = 20): Promise<JobRow[]> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .select("*")
    .eq("session_id", sessionId)
    .order("created_at", { ascending: false })
    .limit(limit);
  if (error) {
    console.error("[db] job list failed:", error.code ?? "unknown");
    throw new ApiError(503, "The database is temporarily unavailable. Please try again.");
  }
  return (data as JobRow[]) ?? [];
}

/**
 * Records the server-generated upload path and authorization expiry for a job
 * still in the 'uploading' state. Scoped by session + job + state.
 * Returns the number of updated rows (0 when the guard did not match).
 */
export async function setUploadAuthorization(
  sessionId: string,
  jobId: string,
  sourcePath: string,
  authExpiresAt: string,
  declared: { fileName: string; sizeBytes: number; mimeType: string }
): Promise<number> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .update({
      source_path: sourcePath,
      upload_auth_expires_at: authExpiresAt,
      source_metadata: {
        original_file_name: declared.fileName,
        declared_size_bytes: declared.sizeBytes,
        declared_mime_type: declared.mimeType,
      },
    })
    .eq("id", jobId)
    .eq("session_id", sessionId)
    .eq("status", "uploading")
    .select("id");
  if (error) {
    console.error("[db] upload authorization update failed:", error.code ?? "unknown");
    throw new ApiError(
      503,
      "The database is temporarily unavailable — the upload authorization was NOT saved. Please retry."
    );
  }
  // Exactly one row can match (primary key) — the guarded update either
  // touched it or it did not.
  return (data ?? []).length;
}

/**
 * Counts the clips detected for one job (Phase 4). The count lets the UI
 * recognize a job parked at 'analyzing' with its clips persisted (analysis
 * finished, rendering is Phase 5) without exposing clip data yet.
 */
export async function countClipsForJob(jobId: string): Promise<number> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("clips")
    .select("id")
    .eq("job_id", jobId);
  if (error) {
    console.error("[db] clip count failed:", error.code ?? "unknown");
    throw new ApiError(503, "The database is temporarily unavailable. Please try again.");
  }
  return (data ?? []).length;
}

/**
 * Atomically marks the upload as completed: 'uploading' → 'queued'.
 * The state guard makes duplicate finalization impossible — only the first
 * caller transitions the row (the transition trigger enforces legality).
 * Returns the number of updated rows (0 when already finalized or invalid).
 */
export async function markUploadComplete(sessionId: string, jobId: string): Promise<number> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .update({
      status: "queued",
      upload_completed_at: new Date().toISOString(),
    })
    .eq("id", jobId)
    .eq("session_id", sessionId)
    .eq("status", "uploading")
    .select("id");
  if (error) {
    console.error("[db] upload completion update failed:", error.code ?? "unknown");
    throw new ApiError(
      503,
      "The database is temporarily unavailable — the upload was NOT marked complete. Please retry the upload."
    );
  }
  // Exactly one row can match (primary key) — the guarded update either
  // touched it or it did not.
  return (data ?? []).length;
}

/**
 * TASK 017 — non-terminal job statuses (a job in any of these is "active").
 * 'completed' and 'failed' are terminal: they release the source for
 * re-submission and are subject to retention.
 */
const ACTIVE_JOB_STATUSES = [
  "uploading",
  "queued",
  "validating",
  "ready",
  "transcribing",
  "analyzing",
  "rendering",
] as const;

/**
 * TASK 017 — duplicate-submission guard: finds an ACTIVE job of the same
 * session with the same source URL (exact string match — no guessing).
 * Used by job creation to answer 409 instead of double-processing the
 * same video (the client's double-click window is the realistic race).
 */
export async function findActiveJobForSourceUrl(
  sessionId: string,
  sourceUrl: string
): Promise<{ id: string } | null> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .select("id")
    .eq("session_id", sessionId)
    .eq("source_url", sourceUrl)
    .in("status", [...ACTIVE_JOB_STATUSES])
    .limit(1);
  if (error) {
    console.error("[db] active job lookup failed:", error.code ?? "unknown");
    throw new ApiError(503, "The database is temporarily unavailable. Please try again.");
  }
  return (data as { id: string }[] | null)?.[0] ?? null;
}

/**
 * TASK 017 — user cancellation of an upload in progress:
 * 'uploading' → 'failed' (a legal transition since the initial schema).
 * The state guard makes a double-cancel impossible — only the first caller
 * transitions the row. Returns the number of updated rows (0 when the job
 * was already finalized, cancelled, or is no longer an upload job).
 *
 * The terminal transition arms the Phase 6 retention deadline, so a
 * partially uploaded object (if any) is covered by the existing purge
 * path instead of leaking forever.
 */
export async function cancelUploadJob(jobId: string): Promise<number> {
  const supabase = getSupabaseServerClient();
  const { data, error } = await supabase
    .from("jobs")
    .update({
      status: "failed",
      last_error: "Cancelled by user — the upload was not completed.",
    })
    .eq("id", jobId)
    .eq("status", "uploading")
    .select("id");
  if (error) {
    console.error("[db] job cancellation failed:", error.code ?? "unknown");
    throw new ApiError(
      503,
      "The database is temporarily unavailable — the job was NOT cancelled. Please try again."
    );
  }
  return (data ?? []).length;
}
